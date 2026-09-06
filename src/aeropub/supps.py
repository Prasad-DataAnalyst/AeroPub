"""ENR 1.8 — where the Annex is not what you are flying under.

A crew's manuals carry ICAO Annex 2 and the PANS. A regional supplementary
procedure (Doc 7030) is what a *region* does differently, and ENR 1.8 is where
a State says which of those apply in its airspace and where it departs from
them again. Three layers, and only the bottom one is in the manual.

That is the whole point of this section. Somebody planning from the Annex alone
is not slightly out of date, they are reading a document that does not govern
the airspace they are in — and nothing about the flight will tell them so.

The finding is the boundary, not the list
------------------------------------------
Six regions' procedures are a table nobody reads. Where the procedure
**changes** is a place, and a place is actionable — the same reasoning
:class:`aeropub.airspace.ClassTransition` and the altimetry section already
follow. So the screen reports where lateral offset stops being permitted,
where the position-reporting interval changes, where the contingency procedure
is not the one used in the region behind.

A boundary with one side unread is not a boundary where nothing changes. It is
one nobody can speak for, and it is reported as that.

Two things it will not do
-------------------------
It does not decide whether a difference matters to a particular operator. That
depends on their operations manual, their approvals and their fleet, none of
which this holds. It reports what the State published and where it changes.

And it never reads silence as agreement. A region whose ENR 1.8 has not been
read publishes nothing here, and "nothing published" and "publishes that it
follows the region" are opposite answers to whether the Annex is enough.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from aeropub.entities import normalise
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)

__all__ = [
    "Applicability",
    "ProcedureArea",
    "ProcedureChange",
    "SuppsRegister",
    "SuppsView",
    "SupplementaryProcedure",
    "load_supps",
    "supps_template",
    "view_supps",
]

#: The parser identity written into citations read from an ENR 1.8 manifest.
SUPPS_PARSER_ID = "aeropub.supps"


class ProcedureArea(str, Enum):
    """What a supplementary procedure is about.

    A closed list, because the screen compares one region's entry against the
    next region's entry for the *same* area, and that comparison is only sound
    if both sides mean the same thing. A procedure that fits none of these is
    ``OTHER`` and is reported rather than compared: two entries both labelled
    "other" are not evidence about each other.
    """

    RVSM = "rvsm"
    SEPARATION = "separation"
    LATERAL_OFFSET = "lateral_offset"
    """SLOP. Permitted in some regions, not in others, and a crew that offsets
    where it is not permitted is off its cleared track."""

    MACH_TECHNIQUE = "mach_technique"
    POSITION_REPORTING = "position_reporting"
    CONTINGENCY = "contingency"
    """What to do when you cannot hold your level or track. The one nobody
    reads until the day they need it, and the one that differs most."""

    COMMUNICATION_FAILURE = "communication_failure"
    WEATHER_DEVIATION = "weather_deviation"
    DATALINK = "datalink"
    SURVEILLANCE = "surveillance"
    FLIGHT_PLANNING = "flight_planning"
    SEARCH_AND_RESCUE = "search_and_rescue"
    OTHER = "other"

    @property
    def is_comparable(self) -> bool:
        """Whether two regions' entries for this area may be compared.

        False for :attr:`OTHER`: two entries both labelled "other" are not
        about the same thing, and reporting a change between them would be a
        finding about our own labelling.
        """
        return self is not ProcedureArea.OTHER

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


class Applicability(str, Enum):
    """What the State says about a regional procedure."""

    APPLIED = "applied"
    """The State applies it as the region publishes it."""

    DIFFERS = "differs"
    """The State publishes something different. The reason this section
    exists."""

    NOT_APPLICABLE = "not_applicable"
    """Published as not applying here — which is itself a difference from a
    crew's assumption, and is not silence."""

    NOT_STATED = "not_stated"
    """The extract carried the procedure without saying which. Reported as not
    knowing."""

    @property
    def departs_from_the_region(self) -> bool | None:
        """Whether following the regional text alone would be wrong here.

        ``None`` where the extract did not say, because assuming either way is
        the failure this section exists to prevent.
        """
        if self is Applicability.NOT_STATED:
            return None
        return self in (Applicability.DIFFERS, Applicability.NOT_APPLICABLE)


@dataclass(frozen=True, slots=True)
class SupplementaryProcedure:
    """One statement of ENR 1.8, as the State publishes it."""

    region: str
    area: ProcedureArea
    source: SourceRef
    icao_region: str = ""
    """The ICAO air navigation region whose Doc 7030 this belongs to — MID,
    EUR, NAT. Held as printed, never inferred from where the FIR is."""

    applicability: Applicability = Applicability.NOT_STATED
    summary: str = ""
    """What the State published, in its own words. The comparison downstream
    is between two of these, so a paraphrase here would compare paraphrases."""

    reference: str = ""
    """The paragraph of Doc 7030 or of the Annex it relates to."""

    conditions: str = ""
    applies_to: str = ""
    """Which operations it binds, where the State narrows it."""

    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(
            self, "icao_region", str(self.icao_region).strip().upper()
        )
        if not self.region:
            raise ValueError(
                "SupplementaryProcedure.region must be a non-empty string. A "
                "procedure with no airspace attached would be read as global, "
                "which is the one thing a supplementary procedure never is."
            )
        if not isinstance(self.area, ProcedureArea):
            raise TypeError("SupplementaryProcedure.area must be a ProcedureArea")
        if not isinstance(self.applicability, Applicability):
            raise TypeError(
                "SupplementaryProcedure.applicability must be an Applicability"
            )
        if not isinstance(self.source, SourceRef):
            raise TypeError("SupplementaryProcedure.source must be a SourceRef")

    @property
    def is_difference(self) -> bool:
        return self.applicability is Applicability.DIFFERS

    def describe(self) -> str:
        parts = [f"{self.region} {self.area.label}"]
        if self.icao_region:
            parts.append(self.icao_region)
        parts.append(self.applicability.value.replace("_", " "))
        if self.summary:
            parts.append(self.summary)
        if self.reference:
            parts.append(self.reference)
        if self.applies_to:
            parts.append(f"applies to {self.applies_to}")
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class ProcedureChange:
    """Where a procedure is not the same on both sides of a boundary.

    The finding. Six regions' procedures are a table; the place a rule changes
    is somewhere a crew has to do something.
    """

    leaving: str
    entering: str
    area: ProcedureArea
    before: SupplementaryProcedure | None = None
    after: SupplementaryProcedure | None = None
    before_read: bool = True
    after_read: bool = True
    """Whether each side's ENR 1.8 was read at all.

    Kept apart from whether a statement was found, because the two absences
    are different findings. A region that was read and publishes nothing about
    lateral offset has answered — the procedure stops being published, and
    that is something a crew acts on. A region nobody read has not.
    """

    @property
    def is_speakable(self) -> bool:
        """Whether both sides were read, statement or no statement."""
        return self.before_read and self.after_read

    @property
    def is_known(self) -> bool:
        """Whether both sides publish a statement for this area."""
        return self.before is not None and self.after is not None

    @property
    def changes_applicability(self) -> bool:
        """Whether the *standing* of the procedure changes, not only its words.

        Applied on one side and differing on the other is the sharper finding:
        a crew following the regional text is right up to the boundary and
        wrong after it.
        """
        if not self.is_known:
            return False
        return self.before.applicability is not self.after.applicability

    def describe(self) -> str:
        where = f"{self.leaving} → {self.entering}"
        if not self.is_speakable:
            missing = self.leaving if not self.before_read else self.entering
            return (
                f"{where}: {self.area.label} — no ENR 1.8 read for {missing}, "
                "so nobody can say whether it changes here"
            )
        if not self.is_known:
            # Both read, one silent. That is an answer: the procedure stops
            # being published, and a crew carrying it across the boundary is
            # carrying something the next State did not publish.
            held = self.before if self.after is None else self.after
            silent = self.entering if self.after is None else self.leaving
            return (
                f"{where}: {self.area.label} — {silent} was read and publishes "
                f"nothing for it, while the other side does"
                + (f" ({held.summary})" if held is not None and held.summary else "")
            )
        return (
            f"{where}: {self.area.label} changes — "
            f"{self.before.applicability.value.replace('_', ' ')} to "
            f"{self.after.applicability.value.replace('_', ' ')}"
            + (f". Entering: {self.after.summary}" if self.after.summary else "")
        )


@dataclass(frozen=True, slots=True)
class SuppsRegister:
    """Every ENR 1.8 statement read so far, and where it was read.

    ``covers`` carries the weight it does everywhere else here: a region in it
    has been read, one not in it has not, and silence is never agreement.
    """

    procedures: tuple[SupplementaryProcedure, ...] = ()
    covers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "covers",
            frozenset(normalise(r) for r in self.covers if normalise(r))
            | {p.region for p in self.procedures},
        )

    def __len__(self) -> int:
        return len(self.procedures)

    def __iter__(self):
        return iter(self.procedures)

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted(self.covers))

    def is_read(self, region: str) -> bool:
        return normalise(region) in self.covers

    def in_region(self, region: str) -> tuple[SupplementaryProcedure, ...]:
        wanted = normalise(region)
        if not wanted:
            return ()
        return tuple(p for p in self.procedures if p.region == wanted)

    def at(
        self, region: str, area: ProcedureArea
    ) -> SupplementaryProcedure | None:
        """One region's statement about one area, where it made one.

        The first held. A State publishing two statements for the same area is
        publishing two, and :meth:`in_region` is how you see both — but the
        comparison across a boundary needs one, and taking the first is what a
        reader working down the page does.
        """
        return next((p for p in self.in_region(region) if p.area is area), None)


@dataclass(frozen=True, slots=True)
class SuppsView:
    """What the crossed regions publish, and where it changes between them."""

    regions: tuple[str, ...] = ()
    procedures: tuple[SupplementaryProcedure, ...] = ()
    unread_regions: tuple[str, ...] = ()
    changes: tuple[ProcedureChange, ...] = ()

    @property
    def is_conclusive(self) -> bool:
        return not self.unread_regions

    @property
    def differences(self) -> tuple[SupplementaryProcedure, ...]:
        """Everything published as departing from the region or the Annex."""
        return tuple(
            p
            for p in self.procedures
            if p.applicability.departs_from_the_region
        )

    @property
    def known_changes(self) -> tuple[ProcedureChange, ...]:
        return tuple(c for c in self.changes if c.is_known)

    @property
    def unspeakable_boundaries(self) -> tuple[ProcedureChange, ...]:
        """Boundaries with one side unread. Not boundaries where nothing
        changes."""
        return tuple(c for c in self.changes if not c.is_speakable)

    @property
    def one_sided(self) -> tuple[ProcedureChange, ...]:
        """Both sides read, one silent. The procedure stops being published."""
        return tuple(
            c for c in self.changes if c.is_speakable and not c.is_known
        )

    def render(self) -> str:
        lines = [
            "SUPPLEMENTARY PROCEDURES — where the Annex is not what you are "
            "flying under",
            f"{len(self.regions)} regions  ·  {len(self.procedures)} published "
            f"statements  ·  {len(self.differences)} depart from the region",
        ]
        if self.unread_regions:
            lines += [
                "",
                f"!! no ENR 1.8 has been read for "
                f"{', '.join(self.unread_regions)}. Whether the Annex governs",
                "   there is not something the held documents answer.",
            ]
        if self.known_changes:
            lines += ["", "WHERE THE PROCEDURE CHANGES"]
            for change in self.known_changes:
                lines.append(f"  {change.describe()}")
        if self.one_sided:
            lines += ["", "PUBLISHED ON ONE SIDE ONLY"]
            for change in self.one_sided:
                lines.append(f"  {change.describe()}")
        if self.unspeakable_boundaries:
            lines += ["", "BOUNDARIES NOBODY CAN SPEAK FOR"]
            for change in self.unspeakable_boundaries:
                lines.append(f"  {change.describe()}")
        if self.differences:
            lines += ["", "DEPARTS FROM THE REGIONAL PROCEDURE"]
            for procedure in self.differences:
                lines.append(f"  {procedure.describe()}")
        return "\n".join(lines)


def view_supps(
    register: SuppsRegister,
    *,
    regions: Iterable[str],
    areas: Iterable[ProcedureArea] = (),
) -> SuppsView:
    """What ENR 1.8 says across these regions, in order of overflight.

    ``regions`` is in order, and the order matters for the same reason it does
    in a route dossier: a change is a thing that happens between two
    consecutive regions, and a set has no betweens.

    ``areas`` narrows the comparison; empty compares every area either side
    publishes.
    """
    wanted = tuple(normalise(r) for r in regions if normalise(r))
    # Consecutive duplicates would produce a boundary between a region and
    # itself, which is never a finding.
    ordered: list[str] = []
    for region in wanted:
        if not ordered or ordered[-1] != region:
            ordered.append(region)

    held: list[SupplementaryProcedure] = []
    unread: list[str] = []
    for region in dict.fromkeys(ordered):
        if not register.is_read(region):
            unread.append(region)
            continue
        held.extend(register.in_region(region))

    asked = tuple(dict.fromkeys(areas)) or tuple(
        area for area in ProcedureArea if area.is_comparable
    )

    changes: list[ProcedureChange] = []
    for leaving, entering in zip(ordered, ordered[1:]):
        for area in asked:
            if not area.is_comparable:
                continue
            before = register.at(leaving, area)
            after = register.at(entering, area)
            if before is None and after is None:
                # Neither side says anything about this area. That is not a
                # boundary finding; the unread region is reported on its own.
                continue
            if before is not None and after is not None:
                same = (
                    before.applicability is after.applicability
                    and before.summary == after.summary
                )
                if same:
                    continue
            changes.append(
                ProcedureChange(
                    leaving=leaving,
                    entering=entering,
                    area=area,
                    before=before,
                    after=after,
                    before_read=register.is_read(leaving),
                    after_read=register.is_read(entering),
                )
            )

    return SuppsView(
        regions=tuple(ordered),
        procedures=tuple(held),
        unread_regions=tuple(unread),
        changes=tuple(changes),
    )


# --------------------------------------------------------------------------
# Reading an ENR 1.8 manifest
# --------------------------------------------------------------------------


def _enum(enum_type, value: object, *, where: str, field: str):
    try:
        return enum_type(str(value).strip().lower().replace("-", "_").replace(" ", "_"))
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ManifestError(
            f"{where}: {field} must be one of {allowed}. The comparison across "
            "a boundary is only sound if both sides mean the same thing, so "
            "there is no free-text area."
        ) from None


def load_supps(path: Path | str) -> SuppsRegister:
    """Read one ENR 1.8 extract, with every statement cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=SUPPS_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()
    default_icao = str(manifest.get("icao_region", "")).strip()

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

    procedures: list[SupplementaryProcedure] = []
    for index, row in enumerate(rows):
        where = f"{path}: procedures[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which paragraph of ENR 1.8 "
                "this came from."
            )
        try:
            procedures.append(
                SupplementaryProcedure(
                    region=str(row.get("region", default_region)),
                    area=_enum(
                        ProcedureArea, row.get("area"), where=where, field="area"
                    ),
                    source=sub_source(document, locator),
                    icao_region=str(row.get("icao_region", default_icao)),
                    applicability=_enum(
                        Applicability,
                        row.get("applicability", Applicability.NOT_STATED.value),
                        where=where,
                        field="applicability",
                    ),
                    summary=str(row.get("summary", "")).strip(),
                    reference=str(row.get("reference", "")).strip(),
                    conditions=str(row.get("conditions", "")).strip(),
                    applies_to=str(row.get("applies_to", "")).strip(),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return SuppsRegister(
        procedures=tuple(procedures), covers=frozenset(covers)
    )


_SUPPS_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "icao_region": "",
    "covers": [],
    "procedures": [
        {
            "region": "",
            "area": "contingency",
            "icao_region": "",
            "applicability": "not_stated",
            "summary": "",
            "reference": "",
            "conditions": "",
            "applies_to": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def supps_template() -> str:
    """A blank ENR 1.8 extract.

    ``covers`` lists every region this extract was read for, including any
    that publish no supplementary procedures at all. Without it, a State that
    follows the region exactly is indistinguishable from one nobody has read,
    and those are opposite answers to whether the Annex is enough.

    ``summary`` is the State's own words. What the screen compares across a
    boundary is two of these, so a paraphrase here compares paraphrases.

    ``area`` comes from a closed list because the comparison is only sound if
    both sides mean the same thing. Anything that fits none of them is
    ``other``, and entries labelled ``other`` are reported rather than
    compared.
    """
    return json.dumps(_SUPPS_TEMPLATE, indent=2)
