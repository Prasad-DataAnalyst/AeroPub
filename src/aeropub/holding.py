"""ENR 1.5 and 3.6 — holding, and the entry nobody works out in the descent.

A published holding pattern is six numbers and a direction, and every one of
them is checkable before the aeroplane is anywhere near the fix: the level band
it may be flown in, the speed the State permits there, the outbound timing that
applies at that level, and — the one crews actually get wrong — which of the
three entries their arrival heading puts them in.

The entry is the point of this module
--------------------------------------
Entry sector is not a judgement. ICAO defines it geometrically: a line through
the fix at 70° to the inbound track divides the compass into a 70° teardrop
sector on the holding side, a 110° parallel sector on the other, and the
remaining 180° as direct. Given an inbound track, a turn direction and an
arrival heading, the answer is arithmetic — and it is arithmetic done in the
descent, on the radio, by people who have other things to do.

:func:`entry_for` does it, including the part most quick references leave out:
ICAO recognises a **5° zone of flexibility either side of each sector
boundary**, within which either adjoining entry is acceptable. A heading three
degrees from a boundary has two right answers, and a tool that printed one of
them as *the* answer would be teaching a precision the procedure does not have.

Speed limits are a table, and the table is not universal
---------------------------------------------------------
:data:`ICAO_HOLDING_SPEEDS` is the maximum indicated airspeed by level band
from PANS-OPS. It is in source for the same reason the Annex 14 tables are: it
is a published construction rule rather than a fact about any aerodrome or
aircraft.

Where a State publishes its own limit for a particular pattern, **the published
limit governs and this module uses it**. The ICAO table is the fallback and the
cross-check, and the finding worth having is the one where a State has
published something *lower* than the table — a restriction a crew planning from
the standard alone would not know about.

Some States apply a different table entirely: the United States, notably, uses
200 kt at or below 6 000 ft and 265 kt above 14 000. This module does not try
to know which State is which. It uses what the pattern publishes, falls back to
ICAO, and says which of the two it used — so a reader can tell a checked figure
from an assumed one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.entities import normalise
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)

__all__ = [
    "Entry",
    "EntrySector",
    "FLEXIBILITY_DEG",
    "HoldingFinding",
    "HoldingPattern",
    "HoldingRegister",
    "ICAO_HOLDING_SPEEDS",
    "SpeedBasis",
    "TurnDirection",
    "entry_for",
    "holding_template",
    "load_holding",
    "max_holding_speed_kt",
    "screen_holding",
    "standard_outbound_time_min",
]

#: The parser identity written into citations read from a holding manifest.
HOLDING_PARSER_ID = "aeropub.holding"

#: The zone either side of a sector boundary in which ICAO accepts either
#: adjoining entry. Printing one answer inside it would teach a precision the
#: procedure does not have.
FLEXIBILITY_DEG = 5.0

#: Maximum indicated airspeed in a hold, by level band, from PANS-OPS. Each
#: entry is (upper level of the band in feet, maximum IAS in knots); the last
#: band has no upper level and is expressed as a Mach number, which is why it
#: is not in this table.
#:
#: In source for the same reason the Annex 14 tables are: a published
#: construction rule, not a fact about an aerodrome or an aircraft. Where a
#: State publishes its own limit for a pattern, that limit governs.
ICAO_HOLDING_SPEEDS: tuple[tuple[float, float], ...] = (
    (14000.0, 230.0),
    (20000.0, 240.0),
    (34000.0, 265.0),
)

#: Above the last banded level PANS-OPS gives a Mach number rather than an
#: indicated airspeed, so no knots figure applies and the module says so
#: rather than extending the table.
ICAO_HOLDING_MACH_ABOVE_FT = 34000.0
ICAO_HOLDING_MACH = 0.83

#: Outbound timing by level, from PANS-OPS. One minute at or below the
#: threshold, one and a half above it.
TIMING_THRESHOLD_FT = 14000.0


def max_holding_speed_kt(level_ft: float) -> float | None:
    """The ICAO maximum indicated airspeed at that level, in knots.

    ``None`` above the last banded level, where PANS-OPS gives a Mach number
    instead. Returning a knots figure there would be an invented conversion:
    the indicated airspeed that corresponds to M0.83 depends on temperature,
    which this platform does not hold.
    """
    for ceiling, speed in ICAO_HOLDING_SPEEDS:
        if level_ft <= ceiling:
            return speed
    return None


def standard_outbound_time_min(level_ft: float) -> float:
    """The outbound timing PANS-OPS applies at that level."""
    return 1.0 if level_ft <= TIMING_THRESHOLD_FT else 1.5


class TurnDirection(str, Enum):
    """Which way the pattern turns. Right is standard."""

    RIGHT = "right"
    LEFT = "left"

    @property
    def is_standard(self) -> bool:
        return self is TurnDirection.RIGHT


class EntrySector(str, Enum):
    """Which of the three entries an arrival heading falls in."""

    DIRECT = "direct"
    """The 180° sector. Cross the fix and turn onto the outbound."""

    PARALLEL = "parallel"
    """The 110° sector on the non-holding side. Cross the fix, turn onto the
    outbound heading on the non-holding side, then turn back."""

    TEARDROP = "teardrop"
    """The 70° sector on the holding side. Cross the fix, turn 30° off the
    outbound on the holding side, then turn to intercept."""


@dataclass(frozen=True, slots=True)
class Entry:
    """The entry for one arrival heading, and whether it is a close call."""

    sector: EntrySector
    heading_deg: float
    inbound_track_deg: float
    turn: TurnDirection
    alternative: EntrySector | None = None
    """The adjoining entry, where the heading is within
    :data:`FLEXIBILITY_DEG` of the boundary between them. ICAO accepts either,
    and a tool that printed one as *the* answer would teach a precision the
    procedure does not have."""

    @property
    def is_boundary(self) -> bool:
        return self.alternative is not None

    def describe(self) -> str:
        text = (
            f"heading {self.heading_deg:03.0f}° into a "
            f"{self.turn.value}-turn hold on {self.inbound_track_deg:03.0f}°: "
            f"{self.sector.value} entry"
        )
        if self.alternative is not None:
            text += (
                f" — within {FLEXIBILITY_DEG:.0f}° of the boundary, so "
                f"{self.alternative.value} is equally acceptable"
            )
        return text


def _bearing(value: float) -> float:
    """Normalise to the range aviation publishes bearings in: 001 to 360.

    North is 360, not 000 — that is the convention every chart and every
    clearance uses, and a module that stored 360 as 0 would print ``000°``
    where the plate says ``360°``. Geometrically identical, and a reader
    checking one against the other sees two different numbers.
    """
    turned = float(value) % 360.0
    return 360.0 if turned == 0.0 else turned


def _sector_for(offset: float, turn: TurnDirection) -> EntrySector:
    """Which sector an offset from the inbound track falls in.

    ``offset`` is the arrival heading minus the inbound track, normalised.
    For a right-turn pattern the holding side is to the right, which puts the
    70° teardrop sector at 110°–180° and the 110° parallel sector at
    180°–290°; the remaining 180° is direct. A left-turn pattern is the
    mirror image, and mirroring rather than re-deriving is deliberate — two
    derivations of one geometry is how the two stop agreeing.
    """
    if turn is TurnDirection.RIGHT:
        if 110.0 <= offset < 180.0:
            return EntrySector.TEARDROP
        if 180.0 <= offset < 290.0:
            return EntrySector.PARALLEL
        return EntrySector.DIRECT
    if 180.0 <= offset < 250.0:
        return EntrySector.TEARDROP
    if 250.0 <= offset < 360.0:
        return EntrySector.PARALLEL
    return EntrySector.DIRECT


def entry_for(
    pattern: "HoldingPattern", heading_deg: float
) -> Entry:
    """Which entry an arrival heading puts you in.

    Arithmetic, not judgement — and arithmetic done in the descent by people
    with other things to do. The 5° flexibility zone either side of each
    boundary is reported rather than resolved: inside it ICAO accepts either
    adjoining entry, and there are two right answers.
    """
    heading = _bearing(heading_deg)
    offset = (heading - pattern.inbound_track_deg) % 360.0
    sector = _sector_for(offset, pattern.turn)

    # A boundary is where the sector on one side differs from the sector on
    # the other. Probing at the edges of the flexibility zone finds them all
    # without the boundaries having to be listed twice — once in _sector_for
    # and once here, which is how they would drift apart.
    below = _sector_for((offset - FLEXIBILITY_DEG) % 360.0, pattern.turn)
    above = _sector_for((offset + FLEXIBILITY_DEG) % 360.0, pattern.turn)
    alternative = None
    for neighbour in (below, above):
        if neighbour is not sector:
            alternative = neighbour
            break

    return Entry(
        sector=sector,
        heading_deg=heading,
        inbound_track_deg=pattern.inbound_track_deg,
        turn=pattern.turn,
        alternative=alternative,
    )


class SpeedBasis(str, Enum):
    """Which limit a speed was checked against.

    Carried so a reader can tell a checked figure from an assumed one. Some
    States apply a table other than ICAO's, and a screen that did not say
    which it used would look equally confident either way.
    """

    PUBLISHED = "published"
    """The pattern's own limit. Governs where it exists."""

    ICAO = "icao"
    """The PANS-OPS table, used where the pattern publishes no limit."""

    NONE = "none"
    """No limit could be established — above the banded levels, where
    PANS-OPS gives a Mach number this platform will not convert."""


@dataclass(frozen=True, slots=True)
class HoldingPattern:
    """One published holding pattern."""

    fix: str
    inbound_track_deg: float
    turn: TurnDirection
    source: SourceRef
    name: str = ""
    region: str = ""
    aerodrome: str = ""
    procedure: str = ""
    """The procedure this hold belongs to, where it belongs to one — a missed
    approach hold is not an en-route hold and a screen should not mix them."""

    minimum_ft: float | None = None
    maximum_ft: float | None = None
    outbound_time_min: float | None = None
    outbound_distance_nm: float | None = None
    """A DME leg length, where the pattern is published by distance rather
    than by timing. The two are alternatives, not a pair."""

    speed_limit_kt: float | None = None
    """The State's own limit for this pattern. Governs where published."""

    purpose: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "fix", normalise(self.fix))
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(self, "aerodrome", normalise(self.aerodrome))
        object.__setattr__(
            self, "inbound_track_deg", _bearing(self.inbound_track_deg)
        )
        if not self.fix:
            raise ValueError("HoldingPattern.fix must be a non-empty string")
        if not isinstance(self.turn, TurnDirection):
            raise TypeError("HoldingPattern.turn must be a TurnDirection")
        if not isinstance(self.source, SourceRef):
            raise TypeError("HoldingPattern.source must be a SourceRef")
        if (
            self.minimum_ft is not None
            and self.maximum_ft is not None
            and self.minimum_ft > self.maximum_ft
        ):
            raise ValueError(
                f"{self.fix}: minimum holding level {self.minimum_ft} is above "
                f"maximum {self.maximum_ft}"
            )
        if (
            self.outbound_time_min is not None
            and self.outbound_distance_nm is not None
        ):
            raise ValueError(
                f"{self.fix}: a pattern is published by timing or by distance, "
                "not both. Holding one of each would let two different "
                "outbound legs both look published."
            )

    @property
    def is_standard_turn(self) -> bool:
        return self.turn.is_standard

    def permits(self, level_ft: float) -> bool | None:
        """Whether the published band contains that level.

        ``None`` where neither bound is held: a pattern whose band nobody read
        cannot be checked, and reporting it as permitted would be a clearance
        this platform is in no position to give.
        """
        if self.minimum_ft is None and self.maximum_ft is None:
            return None
        if self.minimum_ft is not None and level_ft < self.minimum_ft:
            return False
        if self.maximum_ft is not None and level_ft > self.maximum_ft:
            return False
        return True

    def speed_limit_at(self, level_ft: float) -> tuple[float | None, SpeedBasis]:
        """The speed limit that applies, and which authority it came from."""
        if self.speed_limit_kt is not None:
            return (self.speed_limit_kt, SpeedBasis.PUBLISHED)
        standard = max_holding_speed_kt(level_ft)
        if standard is None:
            return (None, SpeedBasis.NONE)
        return (standard, SpeedBasis.ICAO)

    def outbound_at(self, level_ft: float) -> tuple[float | None, str]:
        """The outbound leg that applies at that level, and its unit.

        A pattern published by distance answers in miles at every level. One
        published by timing answers in minutes as published. One publishing
        neither falls back to the PANS-OPS timing for the level, which is what
        a crew would apply — and the caller is told it was a fallback.
        """
        if self.outbound_distance_nm is not None:
            return (self.outbound_distance_nm, "NM")
        if self.outbound_time_min is not None:
            return (self.outbound_time_min, "min")
        return (standard_outbound_time_min(level_ft), "min (PANS-OPS default)")

    def describe(self) -> str:
        parts = [f"{self.fix} hold"]
        if self.name:
            parts.append(self.name)
        parts.append(f"inbound {self.inbound_track_deg:03.0f}°")
        parts.append(f"{self.turn.value} turns")
        if self.minimum_ft is not None or self.maximum_ft is not None:
            low = f"{self.minimum_ft:.0f}" if self.minimum_ft is not None else "?"
            high = f"{self.maximum_ft:.0f}" if self.maximum_ft is not None else "?"
            parts.append(f"{low}-{high} ft")
        if self.outbound_distance_nm is not None:
            parts.append(f"{self.outbound_distance_nm:.0f} NM")
        elif self.outbound_time_min is not None:
            parts.append(f"{self.outbound_time_min:g} min")
        if self.speed_limit_kt is not None:
            parts.append(f"max {self.speed_limit_kt:.0f} kt")
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class HoldingFinding:
    """One thing about holding here that needs a decision."""

    pattern: HoldingPattern
    what: str
    detail: str
    blocking: bool = True
    """Whether it stops the hold being flown as planned, as against needing a
    decision. A level outside the published band is blocking; a non-standard
    turn direction is not — it is flyable and worth knowing."""

    def describe(self) -> str:
        mark = "!!" if self.blocking else " ·"
        return f"{mark} {self.pattern.fix}: {self.what} — {self.detail}"


def screen_holding(
    pattern: HoldingPattern,
    *,
    level_ft: float | None = None,
    speed_kt: float | None = None,
) -> tuple[HoldingFinding, ...]:
    """Check a pattern against the level and speed it will be flown at.

    Each check is skipped rather than guessed where its input is missing, and
    the caller is expected to say so: a screen with no level given has not
    cleared the level band, it has not looked at it.
    """
    findings: list[HoldingFinding] = []

    if not pattern.is_standard_turn:
        findings.append(
            HoldingFinding(
                pattern=pattern,
                what="left turns",
                detail=(
                    "non-standard. The entry sectors and the protected area "
                    "are mirrored, and an entry flown as though it were a "
                    "right-hand pattern leaves the protected side"
                ),
                blocking=False,
            )
        )

    if level_ft is not None:
        permitted = pattern.permits(level_ft)
        if permitted is False:
            low = pattern.minimum_ft
            high = pattern.maximum_ft
            band = (
                f"{low:.0f}" if low is not None else "?"
            ) + " to " + (f"{high:.0f}" if high is not None else "?")
            findings.append(
                HoldingFinding(
                    pattern=pattern,
                    what="level outside the published band",
                    detail=f"{level_ft:.0f} ft against {band} ft",
                )
            )
        elif permitted is None:
            findings.append(
                HoldingFinding(
                    pattern=pattern,
                    what="level band not held",
                    detail=(
                        "nobody read the minimum or maximum holding level, so "
                        "the level was not checked"
                    ),
                    blocking=False,
                )
            )

        if speed_kt is not None:
            limit, basis = pattern.speed_limit_at(level_ft)
            if limit is None:
                findings.append(
                    HoldingFinding(
                        pattern=pattern,
                        what="speed limit not established",
                        detail=(
                            "above the banded levels PANS-OPS gives a Mach "
                            f"number ({ICAO_HOLDING_MACH:g}) rather than an "
                            "indicated airspeed, and converting one needs a "
                            "temperature this platform does not hold"
                        ),
                        blocking=False,
                    )
                )
            elif speed_kt > limit:
                findings.append(
                    HoldingFinding(
                        pattern=pattern,
                        what="above the holding speed limit",
                        detail=(
                            f"{speed_kt:.0f} kt against {limit:.0f} kt "
                            f"({basis.value})"
                        ),
                    )
                )

        # A State publishing below the standard is the finding worth having:
        # a crew planning from PANS-OPS alone would not know about it.
        standard = max_holding_speed_kt(level_ft)
        if (
            pattern.speed_limit_kt is not None
            and standard is not None
            and pattern.speed_limit_kt < standard
        ):
            findings.append(
                HoldingFinding(
                    pattern=pattern,
                    what="published speed limit is below the standard",
                    detail=(
                        f"{pattern.speed_limit_kt:.0f} kt where PANS-OPS "
                        f"allows {standard:.0f} kt at {level_ft:.0f} ft"
                    ),
                    blocking=False,
                )
            )

    return tuple(findings)


@dataclass(frozen=True, slots=True)
class HoldingRegister:
    """Every published pattern read so far."""

    patterns: tuple[HoldingPattern, ...] = ()

    def __len__(self) -> int:
        return len(self.patterns)

    def __iter__(self):
        return iter(self.patterns)

    def at_fix(self, fix: str) -> tuple[HoldingPattern, ...]:
        """Every pattern published at this fix.

        More than one is normal — an en-route hold and a missed approach hold
        can share a fix with different tracks and different bands — so this
        returns all of them rather than picking.
        """
        wanted = normalise(fix)
        if not wanted:
            return ()
        return tuple(p for p in self.patterns if p.fix == wanted)

    def in_region(self, region: str) -> tuple[HoldingPattern, ...]:
        wanted = normalise(region)
        if not wanted:
            return ()
        return tuple(p for p in self.patterns if p.region == wanted)

    def at(self, aerodrome: str) -> tuple[HoldingPattern, ...]:
        wanted = normalise(aerodrome)
        if not wanted:
            return ()
        return tuple(p for p in self.patterns if p.aerodrome == wanted)

    def on_route(self, fixes: Iterable[str]) -> tuple[HoldingPattern, ...]:
        """Patterns published at any of these fixes, in the order given."""
        found: list[HoldingPattern] = []
        for fix in fixes:
            for pattern in self.at_fix(fix):
                if pattern not in found:
                    found.append(pattern)
        return tuple(found)


# --------------------------------------------------------------------------
# Reading a holding manifest
# --------------------------------------------------------------------------


def _number(value: object, *, where: str, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ManifestError(
            f"{where}: {field} {value!r} is not a number. A holding figure "
            "that cannot be read is left unread rather than guessed."
        ) from None


def load_holding(path: Path | str) -> HoldingRegister:
    """Read one holding extract, with every pattern cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=HOLDING_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()
    default_aerodrome = str(manifest.get("aerodrome", "")).strip()

    rows = manifest.get("patterns", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: patterns must be a list")

    patterns: list[HoldingPattern] = []
    for index, row in enumerate(rows):
        where = f"{path}: patterns[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — where this pattern was read "
                "from."
            )
        try:
            turn = TurnDirection(str(row.get("turn", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: turn must be one of "
                f"{', '.join(t.value for t in TurnDirection)}. It decides "
                "which side the protected area is on and which entry applies, "
                "so there is no safe default."
            ) from None

        track = _number(
            row.get("inbound_track_deg"), where=where, field="inbound_track_deg"
        )
        if track is None:
            raise ManifestError(
                f"{where}: inbound_track_deg is required — without it neither "
                "the entry nor the protected side can be worked out."
            )

        try:
            patterns.append(
                HoldingPattern(
                    fix=str(row.get("fix", "")),
                    inbound_track_deg=track,
                    turn=turn,
                    source=sub_source(document, locator),
                    name=str(row.get("name", "")).strip(),
                    region=str(row.get("region", default_region)).strip(),
                    aerodrome=str(row.get("aerodrome", default_aerodrome)).strip(),
                    procedure=str(row.get("procedure", "")).strip(),
                    minimum_ft=_number(
                        row.get("minimum_ft"), where=where, field="minimum_ft"
                    ),
                    maximum_ft=_number(
                        row.get("maximum_ft"), where=where, field="maximum_ft"
                    ),
                    outbound_time_min=_number(
                        row.get("outbound_time_min"),
                        where=where,
                        field="outbound_time_min",
                    ),
                    outbound_distance_nm=_number(
                        row.get("outbound_distance_nm"),
                        where=where,
                        field="outbound_distance_nm",
                    ),
                    speed_limit_kt=_number(
                        row.get("speed_limit_kt"), where=where, field="speed_limit_kt"
                    ),
                    purpose=str(row.get("purpose", "")).strip(),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return HoldingRegister(patterns=tuple(patterns))


_HOLDING_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "aerodrome": "",
    "patterns": [
        {
            "fix": "",
            "name": "",
            "inbound_track_deg": None,
            "turn": "right",
            "minimum_ft": None,
            "maximum_ft": None,
            "outbound_time_min": None,
            "outbound_distance_nm": None,
            "speed_limit_kt": None,
            "procedure": "",
            "purpose": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def holding_template() -> str:
    """A blank holding extract.

    ``inbound_track_deg`` and ``turn`` are both required: without them neither
    the entry nor the protected side can be worked out, and a pattern missing
    either is a row nobody can use. ``outbound_time_min`` and
    ``outbound_distance_nm`` are alternatives — a pattern is published by
    timing or by distance, and holding one of each would let two different
    outbound legs both look published. ``speed_limit_kt`` is the State's own
    limit where it publishes one; left out, the PANS-OPS table applies and the
    screen says which it used.
    """
    return json.dumps(_HOLDING_TEMPLATE, indent=2)
