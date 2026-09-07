"""Aeronautical data quality — accuracy, resolution and integrity.

PANS-AIM (Doc 10066) is where an AIP's shape comes from, and its data
catalogue is the part that matters most to anything analysing one: for every
item an AIP publishes, a required accuracy, a publication resolution, and an
integrity classification.

**The catalogue is not reproduced here.** Doc 10066 is a copyrighted ICAO
publication. What this module holds instead is better suited to the job
anyway: the values **each State publishes for itself**, cited to the section
they were read from — GEN 2.1 and GEN 3 in most AIPs. That is the number a
State can actually be held to, and it is the one a finding can quote back.

Why the State's own figure, and not the ICAO one
-------------------------------------------------
A State that publishes a coarser resolution than the catalogue asks for has
said something, and it is a finding. Screening against a copy of the ICAO
table would produce that finding as *our* claim about *their* data. Screening
against their own published figure produces it as their claim about their own
data, which is the version that survives a conversation with the authority.

Where a State publishes nothing, that is a gap in what is known — not a licence
to substitute a default. :class:`Integrity` and the requirement lookups return
``None`` rather than assuming.

The three integrity classes
----------------------------
Routine, essential and critical, in increasing order of what a corruption
would cost. They are not a severity scale for the *finding* — they say what a
wrong value would do if it were used. A critical item held with no attested
chain is worth reporting precisely because nothing about the value looks
wrong.
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
    "Integrity",
    "DataRequirement",
    "ResolutionFinding",
    "DataQualityRegister",
    "DataQualityView",
    "view_data_quality",
    "load_data_quality",
    "data_quality_template",
    "CATALOGUE_IS_NOT_HELD",
]

DATA_QUALITY_PARSER_ID = "aeropub.dataquality"

CATALOGUE_IS_NOT_HELD = (
    "The PANS-AIM (Doc 10066) data catalogue is a copyrighted ICAO "
    "publication and is not reproduced here. This module holds what each "
    "State publishes for itself, cited to the AIP section it was read from."
)


class Integrity(str, Enum):
    """What a corruption of this item would cost.

    Increasing order. Not a severity scale for a finding — a statement about
    the consequence of the value being wrong.
    """

    ROUTINE = "routine"
    ESSENTIAL = "essential"
    CRITICAL = "critical"

    NOT_CLASSIFIED = "not_classified"
    """The State publishes the item and classifies nothing."""

    UNREAD = "unread"
    """Nobody has read the State's data quality statement."""

    @property
    def is_known(self) -> bool:
        return self not in (Integrity.NOT_CLASSIFIED, Integrity.UNREAD)

    @property
    def rank(self) -> int | None:
        """Order among the classified ones, or ``None`` where unknown.

        ``None`` rather than zero: an unclassified item sorting below a
        routine one would read as safer than the safest class, which is the
        opposite of what not knowing means.
        """
        order = {
            Integrity.ROUTINE: 1,
            Integrity.ESSENTIAL: 2,
            Integrity.CRITICAL: 3,
        }
        return order.get(self)

    def at_least(self, other: "Integrity") -> bool | None:
        """Whether this is at least as demanding as another. ``None`` if
        either is unknown."""
        mine, theirs = self.rank, other.rank
        if mine is None or theirs is None:
            return None
        return mine >= theirs


@dataclass(frozen=True, slots=True)
class DataRequirement:
    """One item's quality, as a State publishes it."""

    item: str
    """The data item, as the State names it — ``"runway threshold"``,
    ``"significant point"``."""

    source: SourceRef
    region: str = ""
    accuracy: str = ""
    """As published, units and all. Held as text because a State writes
    ``1 m``, ``0.5 m`` or ``3 m (95%)`` and the qualifier is part of it."""

    publication_resolution: str = ""
    """The resolution the AIP publishes to — ``1/100 sec``, ``1 sec``."""

    chart_resolution: str = ""
    integrity: Integrity = Integrity.UNREAD
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "item", normalise(self.item))
        object.__setattr__(self, "region", normalise(self.region))
        for name in ("accuracy", "publication_resolution",
                     "chart_resolution", "remarks"):
            object.__setattr__(self, name, str(getattr(self, name)).strip())
        if not self.item:
            raise ValueError("DataRequirement.item must be named")
        if not isinstance(self.integrity, Integrity):
            raise TypeError("DataRequirement.integrity must be an Integrity")
        if not isinstance(self.source, SourceRef):
            raise TypeError("DataRequirement.source must be a SourceRef")

    @property
    def states_anything(self) -> bool:
        """Whether the State published a figure at all."""
        return bool(
            self.accuracy or self.publication_resolution or self.chart_resolution
        ) or self.integrity.is_known

    def describe(self) -> str:
        parts = [self.item]
        if self.accuracy:
            parts.append(f"accuracy {self.accuracy}")
        if self.publication_resolution:
            parts.append(f"published to {self.publication_resolution}")
        if self.integrity.is_known:
            parts.append(f"{self.integrity.value} integrity")
        elif self.integrity is Integrity.NOT_CLASSIFIED:
            parts.append("no integrity class published")
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class ResolutionFinding:
    """A value held more coarsely than the State says it publishes."""

    item: str
    region: str
    held: str
    published_resolution: str
    requirement: DataRequirement

    def describe(self) -> str:
        return (
            f"{self.region} {self.item}: held as “{self.held}” while the "
            f"State publishes to {self.published_resolution} — the coarser "
            "value came from somewhere in our chain, not from the AIP"
        )


@dataclass(frozen=True, slots=True)
class DataQualityRegister:
    """Every data quality statement read, and which regions were read."""

    requirements: tuple[DataRequirement, ...] = ()
    covers: frozenset[str] = frozenset()

    def __len__(self) -> int:
        return len(self.requirements)

    def __iter__(self):
        return iter(self.requirements)

    def is_read(self, region: str) -> bool:
        wanted = normalise(region)
        return wanted in self.covers or any(
            r.region == wanted for r in self.requirements
        )

    def for_item(self, item: str, region: str = "") -> DataRequirement | None:
        """What a State published for one item, or ``None``.

        ``None`` is *not stated*. Nothing substitutes a default: a State that
        published no accuracy for a threshold has not thereby agreed to
        anybody else's.
        """
        wanted_item = normalise(item)
        wanted_region = normalise(region)
        for requirement in self.requirements:
            if requirement.item != wanted_item:
                continue
            if wanted_region and requirement.region != wanted_region:
                continue
            return requirement
        return None

    def integrity_of(self, item: str, region: str = "") -> Integrity:
        found = self.for_item(item, region)
        return found.integrity if found is not None else Integrity.UNREAD


@dataclass(frozen=True, slots=True)
class DataQualityView:
    """What the crossed regions publish about their own data."""

    regions: tuple[str, ...] = ()
    requirements: tuple[DataRequirement, ...] = ()
    unread_regions: tuple[str, ...] = ()
    silent_regions: tuple[str, ...] = ()
    """Read, and publishing no data quality statement."""

    unclassified: tuple[DataRequirement, ...] = ()
    """Published, with no integrity class. Not routine — unstated."""

    findings: tuple[ResolutionFinding, ...] = ()

    @property
    def is_conclusive(self) -> bool:
        return not self.unread_regions

    @property
    def critical_items(self) -> tuple[DataRequirement, ...]:
        return tuple(
            r for r in self.requirements if r.integrity is Integrity.CRITICAL
        )

    def render(self) -> str:
        lines = ["AERONAUTICAL DATA QUALITY — as each State publishes it"]
        for region in self.unread_regions:
            lines.append(f"  {region}: never read")
        for region in self.silent_regions:
            lines.append(f"  {region}: read, and it publishes no data quality statement")
        for requirement in self.requirements:
            lines.append(f"  {requirement.region}  {requirement.describe()}")
        if self.unclassified:
            lines += [
                "",
                "PUBLISHED WITHOUT AN INTEGRITY CLASS",
                "  Unstated, not routine. What a corruption would cost is not "
                "something to assume.",
            ]
            for requirement in self.unclassified:
                lines.append(f"  {requirement.region} {requirement.item}")
        if self.findings:
            lines += ["", "HELD MORE COARSELY THAN PUBLISHED"]
            for finding in self.findings:
                lines.append(f"  {finding.describe()}")
        if self.unread_regions:
            lines += [
                "",
                "  A State whose data quality nobody has read has not thereby "
                "met anybody's.",
            ]
        return "\n".join(lines)


def view_data_quality(
    register: DataQualityRegister,
    *,
    regions: Iterable[str],
    held: Mapping[tuple[str, str], str] | None = None,
) -> DataQualityView:
    """What these regions publish, and where a held value is coarser.

    ``held`` is ``(region, item) -> the value as this platform holds it``,
    supplied by the caller. Nothing is looked up here: this module is about
    what the State published, not about what our store happens to contain.
    """
    wanted = tuple(dict.fromkeys(normalise(r) for r in regions if normalise(r)))

    found: list[DataRequirement] = []
    unread: list[str] = []
    silent: list[str] = []
    for region in wanted:
        if not register.is_read(region):
            unread.append(region)
            continue
        mine = [r for r in register.requirements if r.region == region]
        if not any(r.states_anything for r in mine):
            silent.append(region)
            continue
        found.extend(mine)

    findings: list[ResolutionFinding] = []
    for (region, item), value in (held or {}).items():
        requirement = register.for_item(item, region)
        if requirement is None or not requirement.publication_resolution:
            continue
        if _is_coarser(value, requirement.publication_resolution):
            findings.append(
                ResolutionFinding(
                    item=normalise(item),
                    region=normalise(region),
                    held=str(value),
                    published_resolution=requirement.publication_resolution,
                    requirement=requirement,
                )
            )

    return DataQualityView(
        regions=wanted,
        requirements=tuple(found),
        unread_regions=tuple(unread),
        silent_regions=tuple(silent),
        unclassified=tuple(
            r for r in found if r.integrity is Integrity.NOT_CLASSIFIED
        ),
        findings=tuple(findings),
    )


def _decimals(text: str) -> int | None:
    """Digits after the decimal point in a coordinate's seconds, if any."""
    import re

    match = re.search(r"(\d+)\.(\d+)", str(text))
    return len(match.group(2)) if match else None


def _is_coarser(held: str, published_resolution: str) -> bool:
    """Whether a held value is coarser than a published resolution.

    Deliberately narrow: it compares decimal places in a coordinate's seconds
    and answers ``False`` for anything it cannot compare. A resolution check
    that guessed would produce findings about its own arithmetic.
    """
    import re

    wanted = re.search(r"1/(\d+)\s*sec", published_resolution, re.I)
    if wanted is None:
        return False
    places = len(wanted.group(1)) - 1  # 1/100 sec -> 2 decimal places
    mine = _decimals(held)
    if mine is None:
        # Whole seconds where hundredths are published.
        return bool(re.search(r"\d{6}(\.\d+)?[NSEW]", str(held))) and places > 0
    return mine < places


# --------------------------------------------------------------------------
# Reading a data quality manifest
# --------------------------------------------------------------------------


def _integrity(value: object, *, where: str) -> Integrity:
    try:
        return Integrity(str(value).strip().lower().replace("-", "_").replace(" ", "_"))
    except ValueError:
        allowed = ", ".join(m.value for m in Integrity)
        raise ManifestError(
            f"{where}: integrity must be one of {allowed}. Leave it 'unread' "
            "rather than choosing one — a class nobody published is not "
            "routine."
        ) from None


def load_data_quality(path: Path | str) -> DataQualityRegister:
    """Read one State's published data quality, cited to its own AIP."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=DATA_QUALITY_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()

    covers = manifest.get("covers", [])
    if not isinstance(covers, list):
        raise ManifestError(f"{path}: covers must be a list of regions")
    if default_region:
        covers = list(covers) + [default_region]

    rows = manifest.get("requirements", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: requirements must be a list")

    held: list[DataRequirement] = []
    for index, row in enumerate(rows):
        where = f"{path}: requirements[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which AIP section this was "
                "read from. GEN 2.1 and GEN 3 carry it in most AIPs."
            )
        try:
            held.append(
                DataRequirement(
                    item=str(row.get("item", "")),
                    source=sub_source(document, locator),
                    region=str(row.get("region", default_region)),
                    accuracy=str(row.get("accuracy", "")),
                    publication_resolution=str(row.get("publication_resolution", "")),
                    chart_resolution=str(row.get("chart_resolution", "")),
                    integrity=_integrity(
                        row.get("integrity", Integrity.UNREAD.value), where=where
                    ),
                    remarks=str(row.get("remarks", "")),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return DataQualityRegister(
        requirements=tuple(held), covers=frozenset(covers)
    )


_TEMPLATE = {
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
    "requirements": [
        {
            "item": "",
            "region": "",
            "accuracy": "",
            "publication_resolution": "",
            "chart_resolution": "",
            "integrity": "unread",
            "remarks": "",
            "locator": "",
        }
    ],
}


def data_quality_template() -> str:
    """A blank data quality manifest, with the fields an extract needs."""
    return json.dumps(_TEMPLATE, indent=2)
