"""ENR 1.12 — what happens if a State sends a fighter to look at you.

Interception is rare and its consequences are not recoverable, which is an
awkward combination for a planning platform: the section is skipped because
nothing ever comes of it, and the one time something does, nobody has read it.

The assumption this module exists to refuse
--------------------------------------------
"Everybody follows ICAO Annex 2." Most States publish that they do, and that
published statement is worth having. But a State that has never been read has
not said so, and treating silence as conformance is the one error here whose
cost is not a delay. A crew flying the Annex 2 signals — rocking wings,
flashing navigation lights, the 121.5 call — into a State that publishes its
own is doing the wrong thing confidently, and nothing en route will say so.

So :class:`Conformance` has four states and ``is_annex_2`` returns ``None``
for two of them. An unread ENR 1.12 produces an open item, never a quiet pass.

The finding is the boundary
----------------------------
Six States' interception procedures are a table nobody reads. The place the
procedure *changes* is somewhere a crew has to do something different, and
that is what :func:`view_interception` reports — the same shape ENR 1.8 uses,
for the same reason. Conforming for four regions and departing in the fifth is
a briefing item; five paragraphs of near-identical text is not.

What is held as published, and not judged
------------------------------------------
The frequencies, the signals, the required response and the words a State used
are carried as printed. This module does not decide whether a State's
procedure is reasonable, and it does not translate one State's wording into
another's.

The one derived observation is :attr:`Interception.omits_emergency_frequency`,
and it is narrow on purpose: a State that publishes interception frequencies
without 121.5 among them is worth flagging, because every crew will reach for
121.5 regardless. That is a statement about the published list, not a claim
that the State is wrong.

Force
-----
Some States publish that an aircraft failing to comply may be fired on. That
is a published fact about a route, it is the most consequential thing this
section can carry, and it is kept as its own category rather than folded into
"departs from Annex 2" so it cannot be read past.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.entities import normalise
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)
from aeropub.provenance import SourceRef

__all__ = [
    "Conformance",
    "Departure",
    "Interception",
    "ProcedureBoundary",
    "InterceptionRegister",
    "InterceptionView",
    "view_interception",
    "load_interception",
    "interception_template",
    "EMERGENCY_FREQUENCY_MHZ",
]

INTERCEPTION_PARSER_ID = "aeropub.interception"

#: The international aeronautical emergency frequency (ICAO Annex 10). Held
#: here only to notice when a State's published interception frequencies do
#: not include it — every crew will reach for it regardless.
EMERGENCY_FREQUENCY_MHZ = "121.500"


class Conformance(str, Enum):
    """What the State published about its interception procedures."""

    ANNEX_2 = "annex_2"
    """Published as conforming to ICAO Annex 2. A statement, not a silence."""

    DEPARTS = "departs"
    """Published, and stating something Annex 2 does not."""

    NOT_PUBLISHED = "not_published"
    """ENR 1.12 was read and says nothing about interception."""

    UNREAD = "unread"
    """Nobody has read ENR 1.12 for this region."""

    @property
    def is_annex_2(self) -> bool | None:
        """Whether Annex 2 procedures apply, or ``None`` for not knowing.

        Never ``True`` by default. A State that has not been read has not said
        it conforms, and a crew acting on the assumption that it has is doing
        the wrong thing confidently.
        """
        if self in (Conformance.UNREAD, Conformance.NOT_PUBLISHED):
            return None
        return self is Conformance.ANNEX_2

    @property
    def was_read(self) -> bool:
        return self is not Conformance.UNREAD


class Departure(str, Enum):
    """The kind of thing a State publishes that Annex 2 does not."""

    SIGNALS = "signals"
    """Visual signals other than Annex 2 Appendix 1."""

    FREQUENCY = "frequency"
    RESPONSE = "response"
    """A required response other than the Annex 2 sequence."""

    LISTENING_WATCH = "listening_watch"
    """A continuous listening watch mandated in named airspace."""

    FORCE = "force"
    """A published statement that a non-complying aircraft may be fired on."""

    OTHER = "other"

    @property
    def is_grave(self) -> bool:
        return self is Departure.FORCE


@dataclass(frozen=True, slots=True)
class Interception:
    """One region's ENR 1.12, as published."""

    region: str
    source: SourceRef
    conformance: Conformance = Conformance.UNREAD
    departures: tuple[Departure, ...] = ()
    frequencies: tuple[str, ...] = ()
    """Frequencies the State publishes for interception, as printed."""

    listening_watch: bool | None = None
    """Whether a continuous watch is mandated. ``None`` where not stated —
    which is not the same as not required."""

    listening_watch_airspace: str = ""
    required_response: str = ""
    published_text: str = ""
    """The State's own words for whatever it publishes beyond the Annex."""

    authority: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", normalise(self.region))
        for name in (
            "listening_watch_airspace", "required_response",
            "published_text", "authority", "remarks",
        ):
            object.__setattr__(self, name, str(getattr(self, name)).strip())
        if not self.region:
            raise ValueError("Interception.region must be named")
        if not isinstance(self.conformance, Conformance):
            raise TypeError("Interception.conformance must be a Conformance")
        for departure in self.departures:
            if not isinstance(departure, Departure):
                raise TypeError("Interception.departures must be Departure members")
        if self.departures and self.conformance is Conformance.ANNEX_2:
            raise ValueError(
                f"{self.region}: a departure was recorded but the conformance "
                "says Annex 2. A State publishing something the Annex does not "
                "is departing from it, and recording both would let the "
                "departure be read past."
            )
        if not isinstance(self.source, SourceRef):
            raise TypeError("Interception.source must be a SourceRef")

    @property
    def uses_force(self) -> bool:
        return Departure.FORCE in self.departures

    @property
    def omits_emergency_frequency(self) -> bool:
        """Frequencies published, and 121.5 not among them.

        An observation about the published list, not a finding that the State
        is wrong. It matters because a crew will call on 121.5 whatever the
        page says, and a State that does not monitor it has said so.
        """
        if not self.frequencies:
            return False
        return not any(
            f.strip().rstrip("0").rstrip(".") == EMERGENCY_FREQUENCY_MHZ.rstrip("0").rstrip(".")
            for f in self.frequencies
        )

    def describe_watch(self) -> str:
        """The listening-watch requirement alone.

        Its own sentence rather than the whole entry: an open item whose
        reason repeats every other finding about the region buries the one
        thing it is asking for.
        """
        if not self.listening_watch:
            return ""
        where = self.listening_watch_airspace or self.region
        on = ", ".join(self.frequencies) if self.frequencies else "the published frequency"
        return f"{self.region} requires a continuous watch on {on} in {where}"

    def describe(self) -> str:
        if self.conformance is Conformance.UNREAD:
            return f"{self.region}: ENR 1.12 never read"
        if self.conformance is Conformance.NOT_PUBLISHED:
            return f"{self.region}: ENR 1.12 read, and it publishes no procedure"
        parts = [f"{self.region}"]
        if self.conformance is Conformance.ANNEX_2:
            parts.append("published as conforming to ICAO Annex 2")
        else:
            named = ", ".join(d.value for d in self.departures) or "unstated"
            parts.append(f"departs from Annex 2 ({named})")
        if self.frequencies:
            parts.append("on " + ", ".join(self.frequencies))
        if self.listening_watch:
            where = f" in {self.listening_watch_airspace}" if self.listening_watch_airspace else ""
            parts.append(f"continuous listening watch required{where}")
        if self.uses_force:
            parts.append(
                "THE STATE PUBLISHES THAT A NON-COMPLYING AIRCRAFT MAY BE "
                "FIRED ON"
            )
        if self.published_text:
            parts.append(self.published_text)
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class ProcedureBoundary:
    """Where the interception procedure is not the same on both sides."""

    leaving: str
    entering: str
    before: Interception | None = None
    after: Interception | None = None

    @property
    def is_speakable(self) -> bool:
        """Whether both sides were read at all."""
        return (
            self.before is not None
            and self.after is not None
            and self.before.conformance.was_read
            and self.after.conformance.was_read
        )

    @property
    def enters_a_departure(self) -> bool:
        """Crossing into a State that publishes something the Annex does not.

        The direction matters. A crew conforming to the Annex is right up to
        this line and wrong after it.
        """
        if not self.is_speakable:
            return False
        return (
            self.after.conformance is Conformance.DEPARTS
            and self.before.conformance is not Conformance.DEPARTS
        )

    @property
    def enters_force(self) -> bool:
        if not self.is_speakable:
            return False
        return self.after.uses_force and not self.before.uses_force

    def describe(self) -> str:
        where = f"{self.leaving} → {self.entering}"
        if not self.is_speakable:
            unread = [
                name
                for name, side in ((self.leaving, self.before), (self.entering, self.after))
                if side is None or not side.conformance.was_read
            ]
            return (
                f"{where}: nobody can speak for this boundary — "
                f"{', '.join(unread)} unread"
            )
        if self.enters_force:
            return (
                f"{where}: {self.entering} publishes that a non-complying "
                f"aircraft may be fired on; {self.leaving} does not"
            )
        if self.enters_departure_text():
            return f"{where}: {self.entering} {self.enters_departure_text()}"
        return f"{where}: no published change"

    def enters_departure_text(self) -> str:
        if not self.enters_a_departure:
            return ""
        named = ", ".join(d.value for d in self.after.departures) or "unstated"
        return f"departs from Annex 2 ({named}) where {self.leaving} does not"


@dataclass(frozen=True, slots=True)
class InterceptionRegister:
    """Every ENR 1.12 read, and which regions were read at all."""

    procedures: tuple[Interception, ...] = ()
    covers: frozenset[str] = frozenset()

    def __len__(self) -> int:
        return len(self.procedures)

    def __iter__(self):
        return iter(self.procedures)

    def is_read(self, region: str) -> bool:
        wanted = normalise(region)
        return wanted in self.covers or any(p.region == wanted for p in self.procedures)

    def for_region(self, region: str) -> Interception | None:
        wanted = normalise(region)
        for procedure in self.procedures:
            if procedure.region == wanted:
                return procedure
        return None


@dataclass(frozen=True, slots=True)
class InterceptionView:
    """What ENR 1.12 says across the regions a sector crosses."""

    regions: tuple[str, ...] = ()
    procedures: tuple[Interception, ...] = ()
    unread_regions: tuple[str, ...] = ()
    not_publishing: tuple[str, ...] = ()
    boundaries: tuple[ProcedureBoundary, ...] = ()

    @property
    def is_conclusive(self) -> bool:
        return not self.unread_regions

    @property
    def departures(self) -> tuple[Interception, ...]:
        return tuple(
            p for p in self.procedures if p.conformance is Conformance.DEPARTS
        )

    @property
    def armed(self) -> tuple[Interception, ...]:
        """Regions publishing that a non-complying aircraft may be fired on."""
        return tuple(p for p in self.procedures if p.uses_force)

    @property
    def listening_watch_required(self) -> tuple[Interception, ...]:
        return tuple(p for p in self.procedures if p.listening_watch)

    @property
    def unspeakable_boundaries(self) -> tuple[ProcedureBoundary, ...]:
        return tuple(b for b in self.boundaries if not b.is_speakable)

    @property
    def crossings_into_a_departure(self) -> tuple[ProcedureBoundary, ...]:
        return tuple(b for b in self.boundaries if b.enters_a_departure)

    def render(self) -> str:
        lines = ["INTERCEPTION — ENR 1.12"]
        for region in self.unread_regions:
            lines.append(f"  {region}: never read")
        for region in self.not_publishing:
            lines.append(f"  {region}: read, and it publishes no procedure")
        for procedure in self.procedures:
            lines.append(f"  {procedure.describe()}")

        if self.unread_regions:
            lines += [
                "",
                "NOT READ IS NOT CONFORMING",
                "  A State nobody has read has not published that it follows "
                "Annex 2. The",
                "  signals a crew would fly are the right ones only where a "
                "State says so.",
            ]
        if self.armed:
            lines += ["", "PUBLISHED USE OF FORCE"]
            for procedure in self.armed:
                lines.append(f"  {procedure.region}: {procedure.published_text or 'as published'}")
        if self.crossings_into_a_departure:
            lines += ["", "WHERE THE PROCEDURE CHANGES"]
            for boundary in self.crossings_into_a_departure:
                lines.append(f"  {boundary.describe()}")
        if self.unspeakable_boundaries:
            lines += ["", "BOUNDARIES NOBODY CAN SPEAK FOR"]
            for boundary in self.unspeakable_boundaries:
                lines.append(f"  {boundary.describe()}")
        return "\n".join(lines)


def view_interception(
    register: InterceptionRegister, *, regions: Iterable[str]
) -> InterceptionView:
    """What ENR 1.12 says for these regions, and where it changes between them.

    ``regions`` is taken in the order the sector crosses them, because a
    boundary has a direction: conforming into departing is the finding, and
    the reverse is a crew becoming more conservative than it needs to be.
    """
    wanted = tuple(dict.fromkeys(normalise(r) for r in regions if normalise(r)))

    held: list[Interception] = []
    unread: list[str] = []
    silent: list[str] = []
    by_region: dict[str, Interception | None] = {}

    for region in wanted:
        if not register.is_read(region):
            unread.append(region)
            by_region[region] = None
            continue
        found = register.for_region(region)
        if found is None or found.conformance is Conformance.NOT_PUBLISHED:
            silent.append(region)
            by_region[region] = found
            continue
        held.append(found)
        by_region[region] = found

    boundaries = tuple(
        ProcedureBoundary(
            leaving=leaving,
            entering=entering,
            before=by_region.get(leaving),
            after=by_region.get(entering),
        )
        for leaving, entering in zip(wanted, wanted[1:])
    )

    return InterceptionView(
        regions=wanted,
        procedures=tuple(held),
        unread_regions=tuple(unread),
        not_publishing=tuple(silent),
        boundaries=boundaries,
    )


# --------------------------------------------------------------------------
# Reading an ENR 1.12 manifest
# --------------------------------------------------------------------------


def _enum(enum_type, value: object, *, where: str, field_name: str):
    try:
        return enum_type(
            str(value).strip().lower().replace("-", "_").replace(" ", "_")
        )
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ManifestError(
            f"{where}: {field_name} must be one of {allowed}"
        ) from None


def _tristate(value: object, *, where: str, field_name: str) -> bool | None:
    """Yes, no, or nothing said. A blank is never read as no."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "required", "y"):
        return True
    if text in ("false", "no", "not_required", "n"):
        return False
    raise ManifestError(
        f"{where}: {field_name} must be true, false, or left blank for "
        "'the section does not say'. A blank is not a no — a State that is "
        "silent about a listening watch has not excused you from one."
    )


def load_interception(path: Path | str) -> InterceptionRegister:
    """Read one ENR 1.12 extract, with every statement cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=INTERCEPTION_PARSER_ID,
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

    rows = manifest.get("procedures", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: procedures must be a list")

    procedures: list[Interception] = []
    for index, row in enumerate(rows):
        where = f"{path}: procedures[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which paragraph of ENR 1.12 "
                "this came from."
            )
        try:
            procedures.append(
                Interception(
                    region=str(row.get("region", default_region)),
                    source=sub_source(document, locator),
                    conformance=_enum(
                        Conformance,
                        row.get("conformance", Conformance.UNREAD.value),
                        where=where,
                        field_name="conformance",
                    ),
                    departures=tuple(
                        _enum(Departure, d, where=where, field_name="departures")
                        for d in row.get("departures", [])
                    ),
                    frequencies=tuple(
                        str(f).strip() for f in row.get("frequencies", []) if str(f).strip()
                    ),
                    listening_watch=_tristate(
                        row.get("listening_watch"),
                        where=where,
                        field_name="listening_watch",
                    ),
                    listening_watch_airspace=str(
                        row.get("listening_watch_airspace", "")
                    ).strip(),
                    required_response=str(row.get("required_response", "")).strip(),
                    published_text=str(row.get("published_text", "")).strip(),
                    authority=str(row.get("authority", "")).strip(),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return InterceptionRegister(
        procedures=tuple(procedures), covers=frozenset(covers)
    )


_INTERCEPTION_TEMPLATE = {
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
    "procedures": [
        {
            "region": "",
            "conformance": "annex_2",
            "departures": [],
            "frequencies": ["121.500"],
            "listening_watch": "",
            "listening_watch_airspace": "",
            "required_response": "",
            "published_text": "",
            "authority": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def interception_template() -> str:
    """A blank ENR 1.12 manifest, with the fields an extract needs."""
    return json.dumps(_INTERCEPTION_TEMPLATE, indent=2)
