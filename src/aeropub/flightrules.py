"""ENR 1.3 — which levels this State lets you fly, in which direction.

A cruising level is legal or not depending on where you are pointing, and the
rule that decides it is published per State. Annex 2 Appendix 3 gives the
default — magnetic track 000° to 179° takes one set of levels, 180° to 359°
the other — and States depart from it. They reverse the sectors over a
particular area, they publish the levels in metres, they prescribe a regional
table instead. Every one of those departures is printed in ENR 1.3 and nowhere
else on the flight.

The finding this section exists for
------------------------------------
ENR 3 prints a direction column, and a route that is one-way or that overrides
the semicircular rule says so there. Most segments print nothing, because
nothing about them departs from the State's general rule — and the State's
general rule is in ENR 1.3. Reading the blank column as "any level is fine"
turns the commonest case in the whole route structure into a silent pass. That
was this platform's behaviour until this module existed.

So a segment publishing no direction is :attr:`CruisingLevels.NOT_PUBLISHED`,
which permits nothing and refuses nothing, and the question moves to ENR 1.3
for the region the segment lies in. Where that has not been read either, the
level is unscreened for parity and the document says so.

Magnetic or true is not a detail
---------------------------------
The sectors are drawn on a track, and a track is magnetic or true. Over the
Gulf the variation is two degrees and it makes no difference; over Hudson Bay
it is thirty and it moves a northbound track from one sector to the other,
which changes every legal level on the segment. A State that does not say
which basis it uses has left that open, and this reports it rather than
picking one — no margin is invented here, because the margin that would
matter is the local magnetic variation and that is not something this holds.

Above FL410 the parity rule stops being a parity rule
------------------------------------------------------
Annex 2's table runs on 1000 ft steps to FL410 and 4000 ft steps above it, and
the upper part is not a parity: FL450 sits in the same set as FL290 and FL350
while being an even number of thousands. Anything that answered by parity up
there would clear FL430 eastbound, which is wrong. Above the published parity
ceiling the answer is not known unless the State's own table was read.

Nothing is computed from the track
-----------------------------------
The sector a track falls in is arithmetic on published sector bounds, and that
is all this does. It does not derive a track from two coordinates, does not
apply a variation, and does not decide which level a flight should use. It
answers one question — is the level the flight has filed of the set this State
publishes for this direction — and returns not-known wherever the publication
does not reach.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.airspace import read_limit
from aeropub.ats import CruisingLevels
from aeropub.entities import normalise
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)
from aeropub.provenance import SourceRef

__all__ = [
    "LevelScheme",
    "TrackBasis",
    "CruisingLevelScheme",
    "ParityFinding",
    "FlightRulesRegister",
    "FlightRulesView",
    "view_flight_rules",
    "load_flight_rules",
    "flight_rules_template",
    "ANNEX_2_PARITY_CEILING_FT",
]

FLIGHT_RULES_PARSER_ID = "aeropub.flightrules"
FLIGHT_RULES_PARSER_VERSION = "0.1.0"

#: The level at which Annex 2 Appendix 3 stops being a parity and becomes a
#: table on 4000 ft steps. Above it, parity arithmetic gives wrong answers, so
#: it is where this module stops answering unless a State's own table was read.
ANNEX_2_PARITY_CEILING_FT = 41000.0


class LevelScheme(str, Enum):
    """Which rule the State publishes for cruising levels."""

    SEMICIRCULAR = "semicircular"
    """Annex 2 Appendix 3 table a) — by track, in two sectors."""

    REGIONAL_TABLE = "regional_table"
    """A table prescribed by regional air navigation agreement. Held as a
    scheme this module will not evaluate: the table is the answer and reading
    it out of a paragraph is not something a parser should do."""

    METRIC = "metric"
    """Levels published in metres. The flight levels a crew reads off an
    altimeter are conversions of them, and which conversions a State accepts
    is published rather than arithmetic."""

    NOT_PUBLISHED = "not_published"
    """ENR 1.3 was read and prescribes no scheme."""

    UNREAD = "unread"
    """Nobody has read ENR 1.3 for this region."""

    @property
    def is_by_parity(self) -> bool:
        """Whether a level's parity is the question at all."""
        return self is LevelScheme.SEMICIRCULAR


class TrackBasis(str, Enum):
    """What kind of track the sectors are drawn on."""

    MAGNETIC = "magnetic"
    TRUE = "true"
    GRID = "grid"
    """Used at high latitude where a magnetic track is meaningless."""

    NOT_STATED = "not_stated"

    @property
    def is_stated(self) -> bool:
        return self is not TrackBasis.NOT_STATED


def _bearing(value: float) -> float:
    """A bearing in [0, 360)."""
    return float(value) % 360.0


@dataclass(frozen=True, slots=True)
class CruisingLevelScheme:
    """One State's cruising-level rule, as ENR 1.3 publishes it."""

    region: str
    source: SourceRef
    scheme: LevelScheme = LevelScheme.UNREAD
    basis: TrackBasis = TrackBasis.NOT_STATED

    sector_from_deg: float = 0.0
    sector_to_deg: float = 180.0
    """The arc of tracks taking :attr:`sector_parity`, from inclusive to
    exclusive. Annex 2 draws it 000° to 180°; a State that draws it 090° to
    270° has departed from the Annex and the departure is the reason ENR 1.3
    prints the sectors at all."""

    sector_parity: CruisingLevels = CruisingLevels.ODD
    """Which levels that arc takes. Reversed in some published tables, which
    is why it is held rather than assumed."""

    parity_ceiling_ft: float | None = ANNEX_2_PARITY_CEILING_FT
    """Above this the published levels are not a parity. ``None`` where the
    State published no upper bound, which leaves the upper band unanswered
    rather than answered by arithmetic that does not apply there."""

    minimum_level_ft: float | None = None
    """A minimum flight level or altitude ENR 1.3 states for the region, where
    it states a number. Reported, never used as a floor in place of an MEA —
    a general minimum and a route's own minimum are different statements."""

    minimum_rule: str = ""
    """The minimum stated as a rule rather than a number, printed as printed.
    ``1000 FT above the highest obstacle within 8 KM`` is not evaluable from
    anything held here and is carried so a reader can evaluate it."""

    conditions: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", normalise(self.region))
        for name in ("minimum_rule", "conditions", "remarks"):
            object.__setattr__(self, name, str(getattr(self, name)).strip())
        if not self.region:
            raise ValueError("CruisingLevelScheme.region must be named")
        if not isinstance(self.scheme, LevelScheme):
            raise TypeError("CruisingLevelScheme.scheme must be a LevelScheme")
        if not isinstance(self.basis, TrackBasis):
            raise TypeError("CruisingLevelScheme.basis must be a TrackBasis")
        if not isinstance(self.sector_parity, CruisingLevels):
            raise TypeError(
                "CruisingLevelScheme.sector_parity must be a CruisingLevels"
            )
        if self.sector_parity not in (CruisingLevels.ODD, CruisingLevels.EVEN):
            raise ValueError(
                "CruisingLevelScheme.sector_parity is which of the two sets "
                "the sector takes, so it is odd or even — a sector taking "
                "both or neither is not a semicircular rule"
            )
        object.__setattr__(self, "sector_from_deg", _bearing(self.sector_from_deg))
        object.__setattr__(self, "sector_to_deg", _bearing(self.sector_to_deg))
        if not isinstance(self.source, SourceRef):
            raise TypeError("CruisingLevelScheme.source must be a SourceRef")

    # -- the sectors ------------------------------------------------------

    @property
    def other_parity(self) -> CruisingLevels:
        return (
            CruisingLevels.EVEN
            if self.sector_parity is CruisingLevels.ODD
            else CruisingLevels.ODD
        )

    def in_sector(self, track_deg: float) -> bool:
        """Whether a track falls in the arc taking :attr:`sector_parity`.

        The arc wraps: a State drawing it 270° to 090° means the arc through
        north, not the long way round.
        """
        track = _bearing(track_deg)
        start, end = self.sector_from_deg, self.sector_to_deg
        if start == end:
            return False
        if start < end:
            return start <= track < end
        return track >= start or track < end

    def parity_for(self, track_deg: float | None) -> CruisingLevels | None:
        """Which set of levels this track takes, or ``None`` if unanswerable.

        Unanswerable where ENR 1.3 was never read, where it prescribes
        something other than a semicircular rule, or where no track is held —
        a segment whose track ENR 3 did not print cannot be placed in a
        sector, and placing it in one anyway would be a guess with an AIP's
        authority.
        """
        if not self.scheme.is_by_parity or track_deg is None:
            return None
        return (
            self.sector_parity
            if self.in_sector(track_deg)
            else self.other_parity
        )

    def permits(
        self, level_ft: float, track_deg: float | None
    ) -> bool | None:
        """Whether this level is one this State publishes for this track.

        Three-valued on purpose. ``None`` is *not known* — no scheme read, no
        track held, or a level above the band where parity is the rule — and
        it must never be shown as a pass.
        """
        parity = self.parity_for(track_deg)
        if parity is None:
            return None
        if self.parity_ceiling_ft is None or level_ft > self.parity_ceiling_ft:
            return None
        return parity.permits(level_ft)

    def describe(self) -> str:
        if self.scheme is LevelScheme.UNREAD:
            return f"{self.region}: ENR 1.3 never read"
        if self.scheme is LevelScheme.NOT_PUBLISHED:
            return f"{self.region}: ENR 1.3 read, and it prescribes no scheme"
        parts = [f"{self.region}: {self.scheme.value}"]
        if self.scheme.is_by_parity:
            parts.append(
                f"{self.sector_from_deg:03.0f}°–{self.sector_to_deg:03.0f}° "
                f"takes {self.sector_parity.value} levels"
            )
            parts.append(
                f"on {self.basis.value} track"
                if self.basis.is_stated
                else "on a track whose basis is not stated"
            )
            if self.parity_ceiling_ft is not None:
                parts.append(f"to {self.parity_ceiling_ft:.0f} ft")
        if self.minimum_level_ft is not None:
            parts.append(f"minimum {self.minimum_level_ft:.0f} ft")
        elif self.minimum_rule:
            parts.append(f"minimum: {self.minimum_rule}")
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class ParityFinding:
    """A filed level that is not of the set the State publishes."""

    region: str
    planned_ft: float
    track_deg: float
    expected: CruisingLevels
    scheme: CruisingLevelScheme
    segment: str = ""
    """The route segment this was found on, where it came from one."""

    def describe(self) -> str:
        where = f"{self.segment} " if self.segment else ""
        basis = (
            f"{self.scheme.basis.value} track"
            if self.scheme.basis.is_stated
            else "track (ENR 1.3 does not say magnetic or true)"
        )
        return (
            f"{where}{self.planned_ft:.0f} ft on a {self.track_deg:03.0f}° "
            f"{basis} in {self.region}: ENR 1.3 publishes "
            f"{self.expected.value} levels for this direction"
        )


@dataclass(frozen=True, slots=True)
class FlightRulesRegister:
    """Every ENR 1.3 read, and which regions were read at all."""

    schemes: tuple[CruisingLevelScheme, ...] = ()
    covers: frozenset[str] = frozenset()
    """Regions an ENR 1.3 was read for. A region in here with no scheme was
    read and prescribes nothing; a region in neither has never been read, and
    the two produce different documents."""

    def __len__(self) -> int:
        return len(self.schemes)

    def __iter__(self):
        return iter(self.schemes)

    def is_read(self, region: str) -> bool:
        wanted = normalise(region)
        return wanted in self.covers or any(
            s.region == wanted for s in self.schemes
        )

    def for_region(self, region: str) -> CruisingLevelScheme | None:
        wanted = normalise(region)
        for scheme in self.schemes:
            if scheme.region == wanted:
                return scheme
        return None


@dataclass(frozen=True, slots=True)
class FlightRulesView:
    """What ENR 1.3 says for the regions a sector crosses."""

    regions: tuple[str, ...] = ()
    planned_ft: float | None = None
    schemes: tuple[CruisingLevelScheme, ...] = ()
    unread_regions: tuple[str, ...] = ()
    no_scheme: tuple[str, ...] = ()
    """Read, and prescribing nothing. Not a gap in our reading."""

    findings: tuple[ParityFinding, ...] = ()
    basis_not_stated: tuple[str, ...] = ()
    """Regions whose sectors are drawn on a track of unstated kind. A track
    near a sector boundary takes the other set once the local variation is
    applied, and which set that is cannot be settled from here."""

    unscreened: tuple[str, ...] = ()
    """Segments no parity answer could be given for, with the reason. An
    unscreened segment is not a clear one."""

    @property
    def is_conclusive(self) -> bool:
        return not self.unread_regions and not self.unscreened

    def render(self) -> str:
        lines = ["FLIGHT RULES — ENR 1.3"]
        if self.planned_ft is not None:
            lines.append(f"  planned {self.planned_ft:.0f} ft")
        for region in self.unread_regions:
            lines.append(f"  {region}: never read")
        for region in self.no_scheme:
            lines.append(f"  {region}: read, and it prescribes no scheme")
        for scheme in self.schemes:
            lines.append(f"  {scheme.describe()}")
        if self.basis_not_stated:
            lines += [
                "",
                "TRACK BASIS NOT STATED — " + ", ".join(self.basis_not_stated),
                "  A track near a sector boundary takes the other set of "
                "levels once the local",
                "  magnetic variation is applied, and nothing here holds that "
                "variation.",
            ]
        if self.findings:
            lines += ["", "LEVEL AGAINST DIRECTION"]
            for finding in self.findings:
                lines.append(f"  {finding.describe()}")
        if self.unscreened:
            lines += ["", "NOT SCREENED FOR PARITY"]
            for reason in self.unscreened:
                lines.append(f"  {reason}")
        return "\n".join(lines)


def view_flight_rules(
    register: FlightRulesRegister,
    *,
    regions: Iterable[str],
    planned_ft: float | None = None,
    tracks: Iterable[tuple[str, str, float | None]] = (),
) -> FlightRulesView:
    """What ENR 1.3 says for these regions, against the level as filed.

    ``tracks`` is what ENR 3 published, supplied by the caller as
    ``(segment, region, track_deg)``. It is not derived here: a track computed
    from two coordinates is a great-circle initial bearing, the sectors are
    drawn on a magnetic track flown, and the two differ by the variation plus
    the convergence over the leg.
    """
    wanted = tuple(dict.fromkeys(normalise(r) for r in regions if normalise(r)))

    held: list[CruisingLevelScheme] = []
    unread: list[str] = []
    silent: list[str] = []
    for region in wanted:
        if not register.is_read(region):
            unread.append(region)
            continue
        scheme = register.for_region(region)
        if scheme is None or scheme.scheme is LevelScheme.NOT_PUBLISHED:
            silent.append(region)
            continue
        held.append(scheme)

    by_region = {s.region: s for s in held}
    basis_open = tuple(
        s.region
        for s in held
        if s.scheme.is_by_parity and not s.basis.is_stated
    )

    findings: list[ParityFinding] = []
    unscreened: list[str] = []
    for segment, region, track in tracks:
        name = normalise(segment)
        where = normalise(region)
        scheme = by_region.get(where)
        if scheme is None:
            unscreened.append(
                f"{name}: no ENR 1.3 scheme held for {where or 'its region'}"
            )
            continue
        if track is None:
            unscreened.append(
                f"{name}: ENR 3 published no track, so it cannot be placed "
                "in a sector"
            )
            continue
        if planned_ft is None:
            continue
        answer = scheme.permits(planned_ft, track)
        if answer is None:
            unscreened.append(
                f"{name}: {planned_ft:.0f} ft is above the band where "
                f"{where} publishes levels by parity"
                if scheme.scheme.is_by_parity
                else f"{name}: {where} publishes a {scheme.scheme.value} "
                "scheme, which is a table rather than a parity"
            )
            continue
        if answer is False:
            expected = scheme.parity_for(track)
            if expected is None:  # unreachable while answer is not None
                continue
            findings.append(
                ParityFinding(
                    region=where,
                    planned_ft=planned_ft,
                    track_deg=track,
                    expected=expected,
                    scheme=scheme,
                    segment=name,
                )
            )

    return FlightRulesView(
        regions=wanted,
        planned_ft=planned_ft,
        schemes=tuple(held),
        unread_regions=tuple(unread),
        no_scheme=tuple(silent),
        findings=tuple(findings),
        basis_not_stated=basis_open,
        unscreened=tuple(unscreened),
    )


# --------------------------------------------------------------------------
# Reading an ENR 1.3 manifest
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


def _degrees(value: object, *, where: str, field_name: str) -> float:
    try:
        return _bearing(float(value))
    except (TypeError, ValueError):
        raise ManifestError(
            f"{where}: {field_name} {value!r} is not a bearing in degrees. A "
            "sector bound that cannot be read is left unread, never rounded "
            "to the Annex default — the departure from the Annex is the whole "
            "reason ENR 1.3 prints it."
        ) from None


def load_flight_rules(path: Path | str) -> FlightRulesRegister:
    """Read one ENR 1.3 extract, with every statement cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=FLIGHT_RULES_PARSER_ID,
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

    rows = manifest.get("schemes", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: schemes must be a list")

    schemes: list[CruisingLevelScheme] = []
    for index, row in enumerate(rows):
        where = f"{path}: schemes[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which paragraph of ENR 1.3 "
                "this came from."
            )
        fields = dict(
            region=str(row.get("region", default_region)),
            source=sub_source(document, locator),
            scheme=_enum(
                LevelScheme,
                row.get("scheme", LevelScheme.SEMICIRCULAR.value),
                where=where,
                field_name="scheme",
            ),
            basis=_enum(
                TrackBasis,
                row.get("basis", TrackBasis.NOT_STATED.value),
                where=where,
                field_name="basis",
            ),
            sector_parity=_enum(
                CruisingLevels,
                row.get("sector_parity", CruisingLevels.ODD.value),
                where=where,
                field_name="sector_parity",
            ),
            minimum_level_ft=read_limit(
                row.get("minimum_level"), where=where, field="minimum_level"
            ),
            minimum_rule=str(row.get("minimum_rule", "")).strip(),
            conditions=str(row.get("conditions", "")).strip(),
            remarks=str(row.get("remarks", "")).strip(),
        )
        if row.get("sector_from", "") != "":
            fields["sector_from_deg"] = _degrees(
                row["sector_from"], where=where, field_name="sector_from"
            )
        if row.get("sector_to", "") != "":
            fields["sector_to_deg"] = _degrees(
                row["sector_to"], where=where, field_name="sector_to"
            )
        if "parity_ceiling" in row:
            fields["parity_ceiling_ft"] = read_limit(
                row.get("parity_ceiling"),
                where=where,
                field="parity_ceiling",
            )
        try:
            schemes.append(CruisingLevelScheme(**fields))
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return FlightRulesRegister(
        schemes=tuple(schemes), covers=frozenset(covers)
    )


_FLIGHT_RULES_TEMPLATE = {
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
    "schemes": [
        {
            "region": "",
            "scheme": "semicircular",
            "basis": "not_stated",
            "sector_from": 0,
            "sector_to": 180,
            "sector_parity": "odd",
            "parity_ceiling": "FL410",
            "minimum_level": "",
            "minimum_rule": "",
            "conditions": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def flight_rules_template() -> str:
    """A blank ENR 1.3 manifest, with the fields an extract needs."""
    return json.dumps(_FLIGHT_RULES_TEMPLATE, indent=2)
