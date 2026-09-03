"""Six readers, one body of evidence.

A change to AD 2.13 is one fact. What a first officer needs from it, what an
ops engineer signing a categorisation needs, and what the AIS team chasing a
missing supplement needs are three different documents — and handing all three
the same page means two of them stop reading it.

So a lens is a *view specification*: an audience, the operational domains they
act on, the AD 2 sections they must have before their judgement is sound, and
the order they want it in. It selects and arranges what the dossier, the
bulletin, the forward view and the conduct findings already hold. No lens
computes anything of its own, because six implementations of one calculation
would eventually disagree, and the one that disagreed would be the one somebody
flew on.

The invariant that makes filtering safe
---------------------------------------
**A lens filters findings. It never filters gaps.**

Selecting by domain is exactly how a coverage gap disappears: filter a threat
brief down to what concerns a crew, and the fact that AD 2.10 was never read
concerns nobody, so it vanishes — leaving a clean-looking page about an
aerodrome whose obstacle environment is unknown. Every lens therefore declares
the sections its reader depends on, and any of those that is a gap is shown,
first, whatever the filter says. :attr:`LensView.is_sound` is false while any
remains, and the rendering says so before it says anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from aeropub.aip import DOMAINS, Section, aerodrome_sections, currency_sections, section
from aeropub.bulletin import ChangeBulletin, ReportedChange
from aeropub.dossier import AerodromeDossier, SectionEntry
from aeropub.entities import normalise
from aeropub.horizon import Horizon, Transition
from aeropub.quality import QualityFinding, QualityReport

__all__ = ["LENSES", "Audience", "Lens", "LensView", "lens_for", "view"]


class Audience(str, Enum):
    """Who the document is for."""

    FLIGHT_CREW = "flight_crew"
    AERODROME_STUDY = "aerodrome_study"
    ROUTE_STUDY = "route_study"
    ATS = "ats"
    DISPATCH = "dispatch"
    AIS = "ais"


@dataclass(frozen=True, slots=True)
class Lens:
    """One audience's view of the same evidence."""

    audience: Audience
    title: str
    reader: str
    """Who opens it, in their own job title."""

    purpose: str
    """The decision it exists to support. If a line does not serve this, it
    belongs in another lens."""

    domains: frozenset[str]
    depends_on: tuple[str, ...]
    """AD 2 sections whose absence undermines this reader's judgement.

    Not "sections of interest" — sections without which the document is not
    sound. A threat brief with no obstacle data is not a shorter threat brief."""

    format_note: str = ""
    catches_unclassified: bool = False
    """Whether unclassifiable content lands here.

    A change with no AIP section mapped *and* no impact rule has no domains at
    all, so every domain filter rejects it and it reaches nobody — the exact
    "no rule covers this, so nobody hears about it" failure the bulletin layer
    refuses. It has to land somewhere, and the somewhere is the two readers
    whose remit includes the things nobody has modelled yet."""

    needs_unbuilt: tuple[str, ...] = ()
    """Data this lens wants that the platform does not yet hold. Declared so
    the document says what it is missing rather than quietly omitting it."""

    def __post_init__(self) -> None:
        unknown = set(self.domains) - DOMAINS
        if unknown:
            raise ValueError(
                f"lens {self.audience.value} names unknown domains {sorted(unknown)}"
            )
        for code in self.depends_on:
            section(code)  # raises if it is not a real AIP section

    def admits(self, domains: Iterable[str]) -> bool:
        """Whether something in these domains belongs in this document.

        The fallback, not the primary rule — see :meth:`admits_section`.
        """
        return bool(self.domains & set(domains))

    def admits_section(self, code: str) -> bool:
        """Whether content published in this AIP section belongs in this document."""
        return code in self._required

    def admits_content(self, section_code: str | None, domains: Iterable[str]) -> bool:
        """The selection rule: by section where there is one, by domain otherwise.

        Domains alone are too coarse to select on, and the failure is not
        theoretical — ``dispatch`` covers both aerodrome dispatchability and
        flight planning, so an ATS document filtered on it collects every fire
        category change in the network. ``procedures`` spans ATS procedure and
        runway lighting alike. The section a value is published in is the
        precise statement of what it is, and every lens already names the
        sections its reader depends on.

        Domains remain the fallback for content with no AIP section mapped,
        because losing it entirely would be worse than routing it broadly.
        """
        if section_code is not None:
            return self.admits_section(section_code)
        wanted = set(domains)
        if wanted:
            return self.admits(wanted)
        # Neither a section nor a domain: nobody has classified this. Dropping
        # it is the one outcome that is never acceptable.
        return self.catches_unclassified

    @property
    def _required(self) -> frozenset[str]:
        return frozenset(self.depends_on)

    @property
    def required_sections(self) -> tuple[Section, ...]:
        return tuple(section(code) for code in self.depends_on)


def _codes(*ordinals: int) -> tuple[str, ...]:
    return tuple(f"AD 2.{n}" for n in ordinals)


LENSES: dict[Audience, Lens] = {
    Audience.FLIGHT_CREW: Lens(
        audience=Audience.FLIGHT_CREW,
        title="Threat brief",
        reader="Flight crew",
        purpose="What is different about this aerodrome today, and what will "
        "bite if it is flown the way the last one was.",
        domains=frozenset(
            {"crew", "procedures", "charts", "obstacles", "ground", "noise",
             "navaids", "suitability", "winter"}
        ),
        # Obstacles, runway geometry, lighting and local procedure are what a
        # threat brief is made of; without them it is a page of reassurance.
        # Fire category and winter readiness are here because a crew choosing a
        # diversion needs both, and plan section 21 names RFFS explicitly — it
        # is not only a dispatch concern once the aeroplane is airborne.
        depends_on=_codes(6, 7, 9, 10, 12, 13, 14, 20, 21, 22, 23),
        format_note="One to two pages, EFB-ready.",
    ),
    Audience.AERODROME_STUDY: Lens(
        audience=Audience.AERODROME_STUDY,
        title="Aerodrome study",
        reader="Operations engineering",
        purpose="Whether this aerodrome is suitable for each type and role, "
        "with the constraint that sets each answer named.",
        domains=frozenset(DOMAINS),
        # A controlled document is signed against the whole section set. Any
        # gap invalidates the categorisation it supports.
        depends_on=tuple(s.code for s in aerodrome_sections()),
        catches_unclassified=True,
        format_note="Controlled document, re-signed each cycle.",
    ),
    Audience.ROUTE_STUDY: Lens(
        audience=Audience.ROUTE_STUDY,
        title="Route study",
        reader="Network and operations engineering",
        purpose="Terrain, driftdown, airspace, PBN and EDTO adequacy along a "
        "city pair, and which aerodromes the route actually depends on.",
        domains=frozenset({"airspace", "navaids", "obstacles", "performance",
                           "alternates", "dispatch", "procedures"}),
        depends_on=_codes(6, 11, 12, 13),
        format_note="Study document with terrain profiles.",
        needs_unbuilt=(
            "ENR 3 route facts", "ENR 5.4 en-route obstacles", "terrain data",
        ),
    ),
    Audience.ATS: Lens(
        audience=Audience.ATS,
        title="Airspace and flight plan",
        reader="ATS and ATM",
        purpose="Route availability, level restrictions and whether a filed "
        "plan will validate against what is now in force.",
        domains=frozenset({"airspace", "comms", "navaids"}),
        depends_on=_codes(17, 18, 19, 22),
        format_note="Change list with flight-plan validity flags.",
        needs_unbuilt=("ENR 1 and ENR 3 facts", "RAD and CDR data"),
    ),
    Audience.DISPATCH: Lens(
        audience=Audience.DISPATCH,
        title="Operational digest",
        reader="Dispatch and OCC",
        purpose="Can today's flights go, to here and to the alternates held "
        "against them, and what changes that before the last one lands.",
        domains=frozenset({"dispatch", "performance", "alternates", "suitability",
                           "winter", "met", "noise", "ground"}),
        # Hours, fire category, winter readiness, MET provision and declared
        # distances are the five that decide dispatchability.
        depends_on=_codes(3, 6, 7, 11, 13),
        format_note="Live view, plus a per-flight package.",
    ),
    Audience.AIS: Lens(
        audience=Audience.AIS,
        title="Cycle worklist",
        reader="AIS and AIM team",
        purpose="What the State published, what we hold of it, what is missing, "
        "and what is being carried on the wrong instrument.",
        domains=frozenset({"currency", "regulatory"}),
        # This reader owns completeness, so every section is theirs.
        depends_on=tuple(s.code for s in aerodrome_sections()),
        catches_unclassified=True,
        format_note="Live status board with an audit trail.",
    ),
}


def lens_for(audience: Audience | str) -> Lens:
    """One lens by audience."""
    key = audience if isinstance(audience, Audience) else Audience(str(audience))
    return LENSES[key]


@dataclass(frozen=True, slots=True)
class LensView:
    """One audience's document, assembled and honest about its own gaps."""

    lens: Lens
    entity: str
    as_at: datetime
    changes: tuple[ReportedChange, ...] = ()
    ahead: tuple[Transition, ...] = ()
    conduct: tuple[QualityFinding, ...] = ()
    blocking_gaps: tuple[SectionEntry, ...] = ()
    """Sections this reader depends on that were not read. Never filtered."""

    notams: tuple = ()
    coverage_note: str = ""

    @property
    def is_sound(self) -> bool:
        """Whether this reader can rely on the document.

        False while any section they depend on is unaccounted for. A shorter
        document is not a sound one.
        """
        return not self.blocking_gaps

    @property
    def unannounced(self) -> tuple[Transition, ...]:
        return tuple(t for t in self.ahead if not t.is_announced)

    def summary(self) -> dict[str, object]:
        return {
            "audience": self.lens.audience.value,
            "changes": len(self.changes),
            "ahead": len(self.ahead),
            "unannounced": len(self.unannounced),
            "conduct": len(self.conduct),
            "blocking_gaps": len(self.blocking_gaps),
            "notams": len(self.notams),
            "sound": self.is_sound,
        }

    def render(self) -> str:
        lens = self.lens
        lines = [
            f"{lens.title.upper()} — {self.entity}",
            f"for {lens.reader}  ·  as at {self.as_at:%Y-%m-%d %H:%MZ}",
            "",
            lens.purpose,
            "",
        ]

        # Before anything else. A reader must know whether the page in front of
        # them can carry the decision they are about to make with it.
        if self.blocking_gaps:
            count = len(self.blocking_gaps)
            were = "was" if count == 1 else "were"
            lines += [
                f"NOT SOUND — {count} of the {len(lens.depends_on)} sections this "
                f"document depends on {were} not read. Do not take an absence "
                "below as an all-clear.",
            ]
            lines += [
                f"  {e.section.code:9} {e.section.title}" for e in self.blocking_gaps
            ]
            lines.append("")
        else:
            lines += [
                f"Sound: all {len(lens.depends_on)} sections this document "
                "depends on were read.",
                "",
            ]

        if lens.needs_unbuilt:
            lines += [
                "This lens is partial by construction. Not yet connected: "
                + ", ".join(lens.needs_unbuilt) + ".",
                "",
            ]

        if self.coverage_note:
            lines += [self.coverage_note, ""]

        lines.append("WHAT CHANGED")
        if not self.changes:
            lines.append("  Nothing in this reader's domains.")
        for item in self.changes:
            where = item.section.code if item.section else "unplaced"
            lines.append(f"  [{item.attention.label}] {where}  {item.impact.summary}")
            lines.append(f"      {item.impact.consequence}")

        if self.ahead:
            lines += ["", "WHAT CHANGES NEXT"]
            for item in self.ahead:
                flag = "" if item.is_announced else "   ← nothing will be published"
                lines.append(f"  {item.on:%Y-%m-%d} (T+{item.days_away}) "
                             f"{item.impact.summary}{flag}")

        if self.notams:
            lines += ["", "IN FORCE NOW"]
            for notam, state in self.notams:
                mark = "" if state.is_operative else f"  [{state.value}]"
                lines.append(f"  {notam.identifier}{mark}  {notam.text[:90]}")

        if self.conduct:
            lines += ["", "HOW THIS IS BEING PUBLISHED"]
            for finding in self.conduct:
                lines.append(f"  {finding.describe()}")
                lines.append(f"      {finding.consequence}")

        if lens.format_note:
            lines += ["", lens.format_note]
        return "\n".join(lines)


def view(
    audience: Audience | str,
    entity: str,
    *,
    as_at: datetime,
    dossier: AerodromeDossier | None = None,
    bulletin: ChangeBulletin | None = None,
    ahead: Horizon | None = None,
    conduct: QualityReport | None = None,
) -> LensView:
    """Assemble one audience's document from the evidence supplied.

    Every input is optional. What is not supplied is absent from the document
    and the document says so — an omission that looks like "nothing to report"
    is the failure this whole system is built against.
    """
    lens = lens_for(audience)
    key = normalise(entity)
    if as_at.tzinfo is None:
        raise ValueError("as_at must be timezone-aware (UTC)")

    changes = tuple(
        c
        for c in (bulletin.changes if bulletin else ())
        if lens.admits_content(c.section.code if c.section else None, c.domains)
    )
    upcoming = ()
    if ahead is not None:
        upcoming = tuple(
            t
            for t in ahead.transitions
            if lens.admits_content(
                t.section.code if t.section else None,
                t.impact.domains or (t.section.domains if t.section else ()),
            )
        )

    # Gaps are not filtered. This is the whole reason a lens is safe to use.
    blocking: tuple[SectionEntry, ...] = ()
    if dossier is not None:
        required = set(lens.depends_on)
        blocking = tuple(e for e in dossier.gaps if e.section.code in required)

    findings = tuple(conduct.findings) if conduct is not None else ()

    note = ""
    if bulletin is not None and not bulletin.is_conclusive:
        note = "The change list is not complete: " + bulletin.coverage_statement()

    return LensView(
        lens=lens,
        entity=key,
        as_at=as_at,
        changes=changes,
        ahead=upcoming,
        conduct=findings,
        blocking_gaps=blocking,
        notams=tuple(dossier.notams) if dossier is not None else (),
        coverage_note=note,
    )
