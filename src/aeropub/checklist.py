"""GEN 0.2 / 0.3 / 0.4 — the State's own list, against what we actually hold.

Everything else in this platform answers "what does the AIP say". This module
answers the prior question: **is what we hold the AIP?**

The distinction is not academic. A coverage report built from our own fetch
log says we hold everything we fetched, which is true and useless — it cannot
see the page nobody ever asked for. The State publishes a checklist of every
page in its AIP with the cycle each is current to, and reconciling against
*that* is the difference between "we hold everything we fetched" and "we hold
everything the State says exists". Only the second is a coverage claim.

The finding that matters most is not the missing page
-----------------------------------------------------
It is the **stale** one. A section we do not hold at all is visible everywhere
downstream: every dossier says it was never read. A section we hold at last
cycle looks exactly like a section we hold — it is cited, it renders, it
answers questions, and every answer is one cycle out of date. Nothing in the
page says so. The checklist is the only thing that does.

Two findings point the other way
--------------------------------
**Contradicted.** :class:`aeropub.aip.HoldingState.ABSENT` is a claim about
the State — "it does not publish this" — and it is required to carry its
basis. When the State's own checklist lists the page, that claim is wrong, and
this is where it gets caught. A wrong absence is worse than a gap: a gap is
visible and an absence closes the question.

**Ahead.** We hold a page at a *newer* cycle than the checklist names. That is
not a coverage gap at all — it means the checklist itself is the stale
document, and the thing to refetch is the checklist.

What this does not do
---------------------
It does not decide that a page exists because we hold one. Where a page
identifier cannot be placed against a known section, the entry is reported as
unplaced rather than being forced into the nearest section — a page counted
against the wrong section reconciles something nobody published.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.airac import AiracCycle
from aeropub.aip import (
    AipCoverage,
    HoldingState,
    Section,
    SectionHolding,
    section,
)
from aeropub.entities import normalise
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
    to_date,
)

__all__ = [
    "AmendmentGap",
    "holdings_template",
    "load_holdings",
    "AmendmentRecord",
    "Checklist",
    "ChecklistEntry",
    "PageFinding",
    "PageStatus",
    "Reconciliation",
    "SupplementFinding",
    "SupplementRecord",
    "SupplementStatus",
    "checklist_template",
    "load_checklist",
    "reconcile",
    "sequence_gaps",
]

#: The parser identity written into citations read from a checklist manifest.
CHECKLIST_PARSER_ID = "aeropub.checklist"

#: ``01/26``, ``3/2026`` — an amendment reference as States print it.
_AMENDMENT = re.compile(r"^\s*(?P<ordinal>\d{1,3})\s*/\s*(?P<year>\d{2,4})\s*$")


class PageStatus(str, Enum):
    """One page of the State's checklist, against what we hold."""

    CURRENT = "current"
    """Held, at the cycle the checklist names."""

    STALE = "stale"
    """Held, at an older cycle. The one that renders and answers questions and
    is wrong, with nothing on its face to say so."""

    AHEAD = "ahead"
    """Held at a newer cycle than the checklist names. Not our gap — the
    checklist is the stale document, and it is the thing to refetch."""

    MISSING = "missing"
    """The State lists it and we hold nothing."""

    UNREADABLE = "unreadable"
    """We tried and could not read it. A visible gap, kept apart from never
    having looked because the remedy differs."""

    CONTRADICTED = "contradicted"
    """We recorded it as not published by the State, and the State's own
    checklist lists it. Our claim is wrong."""

    UNPLACED = "unplaced"
    """The page identifier could not be placed against a known section.
    Reported rather than forced: a page counted against the wrong section
    reconciles something nobody published."""

    @property
    def is_coverage_gap(self) -> bool:
        """Whether the State publishes something we cannot brief from."""
        return self in (
            PageStatus.STALE,
            PageStatus.MISSING,
            PageStatus.UNREADABLE,
            PageStatus.CONTRADICTED,
        )

    @property
    def is_current(self) -> bool:
        return self is PageStatus.CURRENT


class SupplementStatus(str, Enum):
    """One supplement, against what we hold."""

    HELD = "held"
    NOT_HELD = "not_held"
    """In force and never received. Indistinguishable from one never issued,
    which is exactly why the record exists."""

    WITHDRAWN = "withdrawn"
    """We hold it and the State's record no longer lists it. Still citable and
    no longer in force, which is the dangerous direction."""

    @property
    def needs_action(self) -> bool:
        return self is not SupplementStatus.HELD


@dataclass(frozen=True, slots=True)
class ChecklistEntry:
    """One line of GEN 0.4 — a page and the cycle it is current to."""

    page: str
    source: SourceRef
    cycle: AiracCycle | None = None
    amendment: str = ""
    """Where the page is non-AIRAC, the amendment that last changed it."""

    section_code: str = ""
    """Given explicitly where the checklist prints one. Otherwise derived from
    the page identifier, and left empty where it cannot be."""

    effective: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "page", str(self.page).strip().upper())
        object.__setattr__(
            self, "section_code", str(self.section_code).strip().upper()
        )
        if not self.page:
            raise ValueError("ChecklistEntry.page must be a non-empty string")
        if not isinstance(self.source, SourceRef):
            raise TypeError("ChecklistEntry.source must be a SourceRef")

    @property
    def section(self) -> Section | None:
        """The section this page belongs to, where it can be established.

        ``None`` rather than a guess. Page identifiers are a State's own
        convention — ``ENR 3.1-5`` places itself and ``AD 2 OTHH-13`` does
        not — and placing a page against the wrong section reconciles
        something nobody published.
        """
        return _place(self.section_code) or _place(self.page)

    def describe(self) -> str:
        parts = [self.page]
        if self.cycle is not None:
            parts.append(f"AIRAC {self.cycle.identifier}")
        if self.amendment:
            parts.append(f"AMDT {self.amendment}")
        return "  ·  ".join(parts)


def _place(text: str) -> Section | None:
    """The section a page identifier belongs to, or nothing.

    Tries the longest prefix that is a known section code and stops. Never
    falls back to the part alone: ``AD 2 OTHH-13`` belongs to some AD 2.x
    subsection and the identifier does not say which, so it is left unplaced.
    """
    candidate = str(text or "").strip().upper()
    if not candidate:
        return None
    # Page identifiers put the page number after a hyphen; the section code is
    # what precedes it.
    head = candidate.split("-")[0].strip()
    while head:
        try:
            return section(head)
        except (KeyError, ValueError):
            pass
        if " " not in head and "." not in head:
            return None
        head = head[:-1].strip()
    return None


@dataclass(frozen=True, slots=True)
class AmendmentRecord:
    """One line of GEN 0.2 — an amendment the State says it issued."""

    identifier: str
    source: SourceRef
    effective: date | None = None
    held: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", str(self.identifier).strip().upper())
        if not self.identifier:
            raise ValueError("AmendmentRecord.identifier must be a non-empty string")

    @property
    def parsed(self) -> tuple[int, int] | None:
        """``(year, ordinal)`` where the reference has the usual shape."""
        found = _AMENDMENT.match(self.identifier)
        if found is None:
            return None
        year = int(found.group("year"))
        if year < 100:
            year += 2000
        return (year, int(found.group("ordinal")))


@dataclass(frozen=True, slots=True)
class AmendmentGap:
    """An amendment number the State's own sequence skips over."""

    year: int
    ordinal: int

    @property
    def identifier(self) -> str:
        return f"{self.ordinal:02d}/{self.year % 100:02d}"

    def describe(self) -> str:
        return (
            f"AMDT {self.identifier} is not in the record and the numbers "
            "either side of it are. Either the State skipped it or we are "
            "reading an incomplete record"
        )


@dataclass(frozen=True, slots=True)
class SupplementRecord:
    """One line of GEN 0.3 — a supplement the State says is in force."""

    identifier: str
    source: SourceRef
    effective: date | None = None
    until: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", str(self.identifier).strip().upper())
        if not self.identifier:
            raise ValueError("SupplementRecord.identifier must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SupplementFinding:
    """One supplement, and whether we can produce it."""

    identifier: str
    status: SupplementStatus
    record: SupplementRecord | None = None

    def describe(self) -> str:
        if self.status is SupplementStatus.NOT_HELD:
            return (
                f"SUP {self.identifier}: in force and never received. A "
                "supplement we never received looks exactly like one never "
                "issued"
            )
        if self.status is SupplementStatus.WITHDRAWN:
            return (
                f"SUP {self.identifier}: held, and no longer in the State's "
                "record. Still citable and no longer in force"
            )
        return f"SUP {self.identifier}: held and in force"


@dataclass(frozen=True, slots=True)
class PageFinding:
    """One page of the checklist, and what we hold against it."""

    page: str
    status: PageStatus
    section_code: str = ""
    listed_cycle: AiracCycle | None = None
    held_cycle: AiracCycle | None = None
    detail: str = ""

    def describe(self) -> str:
        where = f"{self.page}"
        if self.section_code and self.section_code != self.page:
            where += f" ({self.section_code})"
        if self.status is PageStatus.STALE:
            return (
                f"{where}: the State publishes AIRAC "
                f"{self.listed_cycle.identifier if self.listed_cycle else '?'} "
                f"and we hold "
                f"{self.held_cycle.identifier if self.held_cycle else 'no cycle'}"
                " — it renders, it answers, and every answer is out of date"
            )
        if self.status is PageStatus.AHEAD:
            return (
                f"{where}: we hold a newer cycle than the checklist names. "
                "The checklist is the stale document here"
            )
        if self.status is PageStatus.CONTRADICTED:
            return (
                f"{where}: recorded as not published by the State, and the "
                "State's own checklist lists it"
                + (f" — {self.detail}" if self.detail else "")
            )
        if self.status is PageStatus.MISSING:
            return f"{where}: listed by the State and never fetched"
        if self.status is PageStatus.UNREADABLE:
            return f"{where}: listed by the State and could not be read"
        if self.status is PageStatus.UNPLACED:
            return (
                f"{where}: could not be placed against a known section, so "
                "nothing was reconciled for it"
            )
        return f"{where}: current"


@dataclass(frozen=True, slots=True)
class Checklist:
    """One State's published list of its own pages, for one cycle."""

    entity: str
    source: SourceRef
    published_for: AiracCycle | None = None
    entries: tuple[ChecklistEntry, ...] = ()
    amendments: tuple[AmendmentRecord, ...] = ()
    supplements: tuple[SupplementRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity", normalise(self.entity))
        if not self.entity:
            raise ValueError(
                "Checklist.entity must be a non-empty string — whose AIP this "
                "is a checklist of."
            )
        if not isinstance(self.source, SourceRef):
            raise TypeError("Checklist.source must be a SourceRef")

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def sections(self) -> tuple[str, ...]:
        """Every section the checklist places a page in, deduplicated."""
        found = []
        for entry in self.entries:
            placed = entry.section
            if placed is not None and placed.code not in found:
                found.append(placed.code)
        return tuple(found)

    @property
    def unplaced(self) -> tuple[ChecklistEntry, ...]:
        return tuple(e for e in self.entries if e.section is None)


def sequence_gaps(records: Iterable[AmendmentRecord]) -> tuple[AmendmentGap, ...]:
    """Amendment numbers the State's own sequence skips.

    Only between the lowest and highest held in a year: a record that stops at
    04/26 in June says nothing about 05/26 not yet issued, and reporting it
    would produce a finding that appears every cycle and means nothing.
    """
    by_year: dict[int, set[int]] = {}
    for record in records:
        parsed = record.parsed
        if parsed is None:
            continue
        year, ordinal = parsed
        by_year.setdefault(year, set()).add(ordinal)

    gaps: list[AmendmentGap] = []
    for year in sorted(by_year):
        held = by_year[year]
        for ordinal in range(min(held), max(held) + 1):
            if ordinal not in held:
                gaps.append(AmendmentGap(year=year, ordinal=ordinal))
    return tuple(gaps)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the State says exists, against what we hold."""

    entity: str
    checklist: Checklist
    findings: tuple[PageFinding, ...] = ()
    unlisted: tuple[str, ...] = ()
    """Sections we hold that the checklist does not name. Either withdrawn,
    mis-keyed, or the checklist is incomplete — and until somebody says which,
    we are briefing from a page the State does not list."""

    amendment_gaps: tuple[AmendmentGap, ...] = ()
    supplements: tuple[SupplementFinding, ...] = ()

    @property
    def stale(self) -> tuple[PageFinding, ...]:
        return tuple(f for f in self.findings if f.status is PageStatus.STALE)

    @property
    def missing(self) -> tuple[PageFinding, ...]:
        return tuple(
            f
            for f in self.findings
            if f.status in (PageStatus.MISSING, PageStatus.UNREADABLE)
        )

    @property
    def contradicted(self) -> tuple[PageFinding, ...]:
        return tuple(
            f for f in self.findings if f.status is PageStatus.CONTRADICTED
        )

    @property
    def gaps(self) -> tuple[PageFinding, ...]:
        return tuple(f for f in self.findings if f.status.is_coverage_gap)

    @property
    def is_reconciled(self) -> bool:
        """Whether everything the State lists is held at the listed cycle.

        False while anything is stale, missing, contradicted or unplaced. It
        is not a score: a checklist with one unplaced page has not been
        reconciled, however many pages matched.
        """
        return not self.gaps and not self.checklist.unplaced and not self.unlisted

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in PageStatus}
        for finding in self.findings:
            counts[finding.status.value] += 1
        counts["listed"] = len(self.findings)
        return counts

    def render(self) -> str:
        counts = self.counts()
        cycle = self.checklist.published_for
        lines = [
            f"CHECKLIST RECONCILIATION — {self.entity}"
            + (f", checklist for AIRAC {cycle.identifier}" if cycle else ""),
            f"{counts['listed']} pages listed  ·  {counts['current']} current"
            f"  ·  {len(self.gaps)} we cannot brief from"
            f"  ·  {len(self.unlisted)} held and not listed",
        ]
        if self.contradicted:
            lines += [
                "",
                "WE SAID THE STATE DOES NOT PUBLISH THESE, AND IT DOES",
            ]
            for finding in self.contradicted:
                lines.append(f"  {finding.describe()}")
        if self.stale:
            lines += ["", "HELD AT AN OLDER CYCLE THAN THE STATE PUBLISHES"]
            for finding in self.stale:
                lines.append(f"  {finding.describe()}")
        if self.missing:
            lines += ["", "LISTED BY THE STATE AND NOT HELD"]
            for finding in self.missing:
                lines.append(f"  {finding.describe()}")
        ahead = [f for f in self.findings if f.status is PageStatus.AHEAD]
        if ahead:
            lines += ["", "WE HOLD NEWER THAN THE CHECKLIST — REFETCH THE CHECKLIST"]
            for finding in ahead:
                lines.append(f"  {finding.describe()}")
        if self.unlisted:
            lines += [
                "",
                "HELD AND NOT ON THE CHECKLIST — withdrawn, mis-keyed, or the "
                "checklist is incomplete",
            ]
            for code in self.unlisted:
                lines.append(f"  {code}")
        if self.checklist.unplaced:
            lines += ["", "PAGES THAT COULD NOT BE PLACED AGAINST A SECTION"]
            for entry in self.checklist.unplaced:
                lines.append(f"  {entry.describe()}")
        if self.amendment_gaps:
            lines += ["", "AMENDMENT SEQUENCE"]
            for gap in self.amendment_gaps:
                lines.append(f"  {gap.describe()}")
        needs = [s for s in self.supplements if s.status.needs_action]
        if needs:
            lines += ["", "SUPPLEMENTS"]
            for finding in needs:
                lines.append(f"  {finding.describe()}")
        return "\n".join(lines)


def reconcile(
    checklist: Checklist,
    coverage: AipCoverage,
    *,
    held_supplements: Iterable[str] = (),
) -> Reconciliation:
    """The State's own list, against what we hold.

    One finding per listed page, including the ones that match. A report that
    listed only the discrepancies would get shorter as coverage got worse, and
    the count of pages checked is the point of a reconciliation.
    """
    findings: list[PageFinding] = []
    listed_sections: list[str] = []

    for entry in checklist.entries:
        placed = entry.section
        if placed is None:
            findings.append(
                PageFinding(
                    page=entry.page,
                    status=PageStatus.UNPLACED,
                    listed_cycle=entry.cycle,
                )
            )
            continue
        if placed.code not in listed_sections:
            listed_sections.append(placed.code)

        holding = coverage.holding(checklist.entity, placed.code)
        status, detail = _status_of(holding, entry)
        findings.append(
            PageFinding(
                page=entry.page,
                status=status,
                section_code=placed.code,
                listed_cycle=entry.cycle,
                held_cycle=holding.cycle,
                detail=detail,
            )
        )

    listed = set(listed_sections)
    unlisted = tuple(
        sorted(
            h.section.code
            for h in coverage
            if h.entity == checklist.entity
            and h.state is HoldingState.HELD
            and h.section.code not in listed
        )
    )

    have = {normalise(s) for s in held_supplements if normalise(s)}
    in_force = {r.identifier: r for r in checklist.supplements}
    supplements: list[SupplementFinding] = []
    for identifier, record in in_force.items():
        supplements.append(
            SupplementFinding(
                identifier=identifier,
                status=(
                    SupplementStatus.HELD
                    if identifier in have
                    else SupplementStatus.NOT_HELD
                ),
                record=record,
            )
        )
    for identifier in sorted(have - set(in_force)):
        supplements.append(
            SupplementFinding(
                identifier=identifier, status=SupplementStatus.WITHDRAWN
            )
        )

    return Reconciliation(
        entity=checklist.entity,
        checklist=checklist,
        findings=tuple(findings),
        unlisted=unlisted,
        amendment_gaps=sequence_gaps(checklist.amendments),
        supplements=tuple(supplements),
    )


def _status_of(holding, entry: ChecklistEntry) -> tuple[PageStatus, str]:
    """One page's status, from what we hold and what the checklist says."""
    if holding.state is HoldingState.ABSENT:
        return PageStatus.CONTRADICTED, holding.detail
    if holding.state is HoldingState.FAILED:
        return PageStatus.UNREADABLE, holding.detail
    if holding.state is HoldingState.NOT_CHECKED:
        return PageStatus.MISSING, ""
    # Held. The question is which cycle, and a holding with no cycle recorded
    # cannot be shown to be current — which is not the same as being stale, but
    # is the same thing to brief from, so it is reported as stale with the
    # absence named.
    if entry.cycle is None:
        return PageStatus.CURRENT, ""
    if holding.cycle is None:
        return PageStatus.STALE, "no cycle recorded against what we hold"
    if holding.cycle < entry.cycle:
        return PageStatus.STALE, ""
    if holding.cycle > entry.cycle:
        return PageStatus.AHEAD, ""
    return PageStatus.CURRENT, ""


# --------------------------------------------------------------------------
# Reading a checklist manifest
# --------------------------------------------------------------------------


def _cycle(value: object, *, where: str, field_name: str) -> AiracCycle | None:
    if value is None or value == "":
        return None
    try:
        return AiracCycle.from_identifier(str(value))
    except ValueError as error:
        raise ManifestError(f"{where}: {field_name} {error}") from None


def load_checklist(path: Path | str) -> Checklist:
    """Read one GEN 0.2/0.3/0.4 extract, with every line cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=CHECKLIST_PARSER_ID,
    )
    entity = str(manifest.get("entity", "")).strip()
    if not entity:
        raise ManifestError(
            f"{path}: entity is required — whose AIP this is a checklist of."
        )

    entries: list[ChecklistEntry] = []
    for index, row in enumerate(manifest.get("pages", [])):
        where = f"{path}: pages[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which line of the checklist "
                "this came from."
            )
        try:
            entries.append(
                ChecklistEntry(
                    page=str(row.get("page", "")),
                    source=sub_source(document, locator),
                    cycle=_cycle(row.get("cycle"), where=where, field_name="cycle"),
                    amendment=str(row.get("amendment", "")).strip(),
                    section_code=str(row.get("section", "")),
                    effective=to_date(
                        row.get("effective"), where=where, field="effective"
                    ),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    amendments: list[AmendmentRecord] = []
    for index, row in enumerate(manifest.get("amendments", [])):
        where = f"{path}: amendments[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip() or "GEN 0.2"
        try:
            amendments.append(
                AmendmentRecord(
                    identifier=str(row.get("identifier", "")),
                    source=sub_source(document, locator),
                    effective=to_date(
                        row.get("effective"), where=where, field="effective"
                    ),
                    held=bool(row.get("held", False)),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    supplements: list[SupplementRecord] = []
    for index, row in enumerate(manifest.get("supplements", [])):
        where = f"{path}: supplements[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip() or "GEN 0.3"
        try:
            supplements.append(
                SupplementRecord(
                    identifier=str(row.get("identifier", "")),
                    source=sub_source(document, locator),
                    effective=to_date(
                        row.get("effective"), where=where, field="effective"
                    ),
                    until=to_date(row.get("until"), where=where, field="until"),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return Checklist(
        entity=entity,
        source=document,
        published_for=_cycle(
            manifest.get("published_for"),
            where=f"{path}: published_for",
            field_name="published_for",
        ),
        entries=tuple(entries),
        amendments=tuple(amendments),
        supplements=tuple(supplements),
    )


def load_holdings(path: Path | str) -> AipCoverage:
    """Read a record of what we hold, for reconciling a checklist against.

    A stand-in, and deliberately a separate file. Until ingestion records a
    :class:`aeropub.aip.SectionHolding` for every section it reads, the
    alternative is reconciling against an empty coverage — which would report
    every page the State publishes as missing, and a report where everything
    is a finding is a report nobody reads.

    An ``absent`` holding must carry its reason here as everywhere else: it is
    a claim about the State, and this module exists partly to catch the wrong
    ones.
    """
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=CHECKLIST_PARSER_ID,
    )
    entity = str(manifest.get("entity", "")).strip()
    if not entity:
        raise ManifestError(
            f"{path}: entity is required — whose holdings these are."
        )

    rows = manifest.get("holdings", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: holdings must be a list")

    held: list[SectionHolding] = []
    for index, row in enumerate(rows):
        where = f"{path}: holdings[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        code = str(row.get("section", "")).strip()
        if not code:
            raise ManifestError(f"{where}: section is required")
        try:
            found = section(code)
        except (KeyError, ValueError) as error:
            raise ManifestError(f"{where}: {error}") from None
        try:
            state = HoldingState(str(row.get("state", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: state must be one of "
                f"{', '.join(h.value for h in HoldingState)}"
            ) from None
        try:
            held.append(
                SectionHolding(
                    section=found,
                    entity=str(row.get("entity", entity)),
                    state=state,
                    cycle=_cycle(row.get("cycle"), where=where, field_name="cycle"),
                    source=(
                        sub_source(document, str(row.get("locator", "")).strip() or code)
                        if state is HoldingState.HELD
                        else None
                    ),
                    detail=str(row.get("detail", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return AipCoverage(held)


_HOLDINGS_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
    },
    "entity": "",
    "holdings": [
        {
            "section": "",
            "state": "held",
            "cycle": "",
            "detail": "",
            "locator": "",
        }
    ],
}


def holdings_template() -> str:
    """A blank record of what we hold.

    ``state`` is one of held, absent, failed or not_checked. ``absent`` is a
    claim about the State — that it does not publish this section — and needs
    its basis in ``detail``, because a wrong absence closes a question a gap
    would have kept open.
    """
    return json.dumps(_HOLDINGS_TEMPLATE, indent=2)


_CHECKLIST_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "entity": "",
    "published_for": "",
    "pages": [
        {
            "page": "",
            "section": "",
            "cycle": "",
            "amendment": "",
            "effective": "",
            "locator": "",
        }
    ],
    "amendments": [{"identifier": "", "effective": "", "held": False, "locator": ""}],
    "supplements": [{"identifier": "", "effective": "", "until": "", "locator": ""}],
}


def checklist_template() -> str:
    """A blank GEN 0.2/0.3/0.4 extract.

    ``page`` is the identifier as the State prints it. ``section`` is only
    needed where the identifier does not place itself — ``ENR 3.1-5`` does and
    ``AD 2 OTHH-13`` does not, and a page that cannot be placed is reported
    rather than guessed at.

    ``cycle`` is the AIRAC the checklist says that page is current to, in
    ``YYNN`` form. It is the field the whole reconciliation turns on: a page
    held at an older cycle renders, cites and answers, and every answer is out
    of date.
    """
    return json.dumps(_CHECKLIST_TEMPLATE, indent=2)
