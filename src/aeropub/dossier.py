"""The complete aerodrome dossier — everything we hold about one aerodrome.

The *"tell me everything about this"* mode of plan section 1, assembled from
the four things already built rather than invented here:

- :mod:`aeropub.aip` — which sections should exist, and which one publishes
  each attribute;
- :class:`~aeropub.aip.AipCoverage` — what we hold of them, and what we do not;
- :class:`~aeropub.facts.FactStore` — the Consolidated Effective State, so each
  value is the one actually in force rather than whatever the base AIP said;
- :class:`~aeropub.notam_register.NotamRegister` — what is overlaying it now.

The design position that shapes everything below: **a dossier is not a summary
of what we know, it is a statement of what is knowable and where we stand
against it.** Every AD 2 section appears, held or not. A section nobody has
looked at is printed as plainly as one that was read this morning, because the
alternative — a tidy report listing only what we happen to have — is the
failure this whole project exists to avoid. A crew reading a dossier with AD
2.10 quietly missing has no way to know the obstacle data was never checked.

Two things it deliberately does not do. It does not route NOTAM to AIP sections
by reading their text: NOTAM attach to *objects*, structurally, and a runway
NOTAM is shown against the runway rather than guessed into AD 2.12 or AD 2.14.
And it does not assess anything against a fleet — that is layer three, and a
dossier is complete and useful with no operator configured at all.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from aeropub.aip import (
    AipCoverage,
    HoldingState,
    Section,
    aerodrome_sections,
    section_for_attribute,
)
from aeropub.airac import AiracCycle
from aeropub.entities import covers, normalise, scope_of
from aeropub.facts import Fact, FactStore
from aeropub.notam_register import ForceState, NotamRegister, RegisteredNotam

__all__ = ["AerodromeDossier", "SectionEntry", "ValueLine", "build", "build_dossier"]


@dataclass(frozen=True, slots=True)
class ValueLine:
    """One value in force, and where it came from."""

    entity: str
    attribute: str
    fact: Fact

    @property
    def value(self):
        return self.fact.value

    @property
    def scope(self) -> str:
        """``"aerodrome"`` or the object on it the value belongs to."""
        return scope_of(self.entity) or "aerodrome"

    def describe(self) -> str:
        return (
            f"{self.scope} · {self.attribute} = {self.fact.value} "
            f"[{self.fact.precedence.name}]"
        )


@dataclass(frozen=True, slots=True)
class SectionEntry:
    """One AD 2 section as it stands for this aerodrome."""

    section: Section
    state: HoldingState
    values: tuple[ValueLine, ...] = ()
    cycle: AiracCycle | None = None
    detail: str = ""

    @property
    def is_gap(self) -> bool:
        return self.state.is_gap

    @property
    def has_values(self) -> bool:
        return bool(self.values)

    def describe(self) -> str:
        mark = {
            HoldingState.HELD: "  ",
            HoldingState.ABSENT: "--",
            HoldingState.FAILED: "!!",
            HoldingState.NOT_CHECKED: "??",
        }[self.state]
        return f"{mark}  {self.section.code:9} {self.section.title}"


@dataclass(frozen=True, slots=True)
class AerodromeDossier:
    """Everything held about one aerodrome, at one moment, with its gaps."""

    aerodrome: str
    as_at: datetime
    """The moment the dossier speaks for. NOTAM are resolved to this minute."""

    on: date
    """The day the effective state is resolved for."""

    sections: tuple[SectionEntry, ...]
    notams: tuple[tuple[RegisteredNotam, ForceState], ...] = ()
    unplaced: tuple[ValueLine, ...] = ()
    """Values held for this aerodrome that no AIP section has been mapped to.

    Shown rather than dropped. A value filed under a plausible guess reads as
    though that section said it, which is worse than an admitted loose end."""

    cycle: AiracCycle | None = None

    as_known_at: datetime | None = None
    """The moment whose knowledge this was built from, or ``None`` for now.

    Present in the document rather than only in the call, because a printed
    dossier that does not say it is a retrospective view is indistinguishable
    from a current one."""

    # -- views -----------------------------------------------------------

    @property
    def held(self) -> tuple[SectionEntry, ...]:
        return tuple(e for e in self.sections if e.state is HoldingState.HELD)

    @property
    def gaps(self) -> tuple[SectionEntry, ...]:
        """Sections we cannot account for. Ours, not the State's."""
        return tuple(e for e in self.sections if e.is_gap)

    @property
    def absent(self) -> tuple[SectionEntry, ...]:
        """Sections the State genuinely does not publish."""
        return tuple(e for e in self.sections if e.state is HoldingState.ABSENT)

    @property
    def is_complete(self) -> bool:
        """Whether every AD 2 section is accounted for, one way or the other."""
        return not self.gaps

    def section(self, code: str) -> SectionEntry:
        for entry in self.sections:
            if entry.section.code == code.strip().upper():
                return entry
        raise KeyError(f"{code} is not an AD 2 section")

    def values(self) -> tuple[ValueLine, ...]:
        return tuple(v for e in self.sections for v in e.values) + self.unplaced

    def operative_notams(self) -> tuple[tuple[RegisteredNotam, ForceState], ...]:
        return tuple((n, s) for n, s in self.notams if s.is_operative)

    def summary(self) -> dict[str, int]:
        return {
            "sections": len(self.sections),
            "held": len(self.held),
            "absent": len(self.absent),
            "gaps": len(self.gaps),
            "values": len(self.values()),
            "notams": len(self.notams),
            "notams_unresolved": sum(
                1 for _, s in self.notams if s is ForceState.SCHEDULE_UNKNOWN
            ),
        }

    # -- output ----------------------------------------------------------

    def render(self) -> str:
        """A printable dossier. Every section appears, held or not."""
        counts = self.summary()
        cycle = f"  ·  AIRAC {self.cycle.identifier}" if self.cycle else ""
        lines = [
            f"AERODROME DOSSIER — {self.aerodrome}",
            f"as at {self.as_at:%Y-%m-%d %H:%MZ}  ·  effective state on "
            f"{self.on:%Y-%m-%d}{cycle}",
            *(
                [
                    f"RETROSPECTIVE — built from what was known at "
                    f"{self.as_known_at:%Y-%m-%d %H:%MZ}, not from what is known now",
                ]
                if self.as_known_at is not None
                else []
            ),
            "",
            f"{counts['held']} of {counts['sections']} AD 2 sections held  ·  "
            f"{counts['absent']} not published  ·  {counts['gaps']} unaccounted for",
            "",
            "AIP AD 2",
        ]
        for entry in self.sections:
            lines.append(f"  {entry.describe()}")
            for value in entry.values:
                lines.append(f"           {value.describe()}")
                lines.append(f"           {value.fact.source.describe()}")
            if entry.detail:
                lines.append(f"           {entry.detail}")

        if self.unplaced:
            lines += ["", "Held, but not attributed to a section"]
            lines += [f"  {v.describe()}" for v in self.unplaced]

        lines += ["", "NOTAM in force"]
        if not self.notams:
            lines.append(
                "  none indexed for this aerodrome — a coverage gap, "
                "not a quiet aerodrome"
            )
        for notam, state in self.notams:
            mark = "" if state is ForceState.IN_FORCE else f"  [{state.value}]"
            lines.append(f"  {notam.identifier}{mark}")
            for subject in notam.subjects:
                lines.append(f"      {subject.describe()}")
            if notam.text:
                lines.append(f"      {notam.text}")
            if notam.has_schedule:
                lines.append(f"      schedule: {notam.schedule}")
            lines.append(f"      {notam.source.describe()}")

        if self.gaps:
            lines += [
                "",
                "COVERAGE GAPS — these sections were not read, and nothing below "
                "them should be assumed",
            ]
            lines += [f"  {e.section.code:9} {e.section.title}" for e in self.gaps]
        return "\n".join(lines)


def build(
    aerodrome: str,
    *,
    facts: FactStore | None = None,
    coverage: AipCoverage | None = None,
    register: NotamRegister | None = None,
    as_at: datetime | None = None,
    on: date | None = None,
    as_known_at: datetime | None = None,
    cycle: AiracCycle | None = None,
) -> AerodromeDossier:
    """Assemble the dossier for one aerodrome.

    Every argument is optional and every omission is visible in the result: a
    dossier built with no fact store shows every section empty, and one built
    with no register says plainly that no NOTAM were indexed. Nothing is
    silently skipped, because a report that omits what it was not given is
    indistinguishable from one where there was nothing to say.

    Two different dates, and confusing them is the trap
    --------------------------------------------------
    ``on`` is **valid time**: which day the effective state is resolved for.
    ``as_known_at`` is **transaction time**: the moment whose *knowledge* to
    use. Left unset it means now, and the dossier is the corrected record —
    everything we hold today about that day, including a NOTAM that reached us
    three days late.

    Set it, and the dossier becomes what could actually have been produced at
    that moment. For a safety investigation that is the only honest answer:
    "what was in force on the 15th" and "what anybody could have known on the
    15th" are different documents, and reporting the first as the second
    quietly blames a crew for not knowing something nobody had sent them yet.
    """
    moment = as_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("as_at must be timezone-aware (UTC)")
    day = on or moment.date()
    key = normalise(aerodrome)
    if not key:
        raise ValueError("aerodrome must be a non-empty string")

    store = facts or FactStore()
    held = coverage or AipCoverage()

    # Resolve the effective value of every attribute we hold for this
    # aerodrome and anything on it, then file each under the section ICAO
    # publishes it in.
    by_section: dict[str, list[ValueLine]] = defaultdict(list)
    unplaced: list[ValueLine] = []
    for entity in sorted(e for e in store.entities() if covers(key, e)):
        for attribute in sorted(store.attributes(entity)):
            fact = store.effective(entity, attribute, day, as_known_at=as_known_at)
            if fact is None:
                # Nothing in force on this day. Not an error and not a value —
                # the attribute simply has no answer for this date.
                continue
            line = ValueLine(entity=entity, attribute=attribute, fact=fact)
            placed = section_for_attribute(attribute)
            if placed is None:
                unplaced.append(line)
            else:
                by_section[placed.code].append(line)

    entries = []
    for candidate in aerodrome_sections():
        holding = held.holding(key, candidate.code)
        entries.append(
            SectionEntry(
                section=candidate,
                state=holding.state,
                values=tuple(by_section.get(candidate.code, ())),
                cycle=holding.cycle,
                detail=holding.detail,
            )
        )

    notams = register.at(key, moment) if register is not None else ()

    return AerodromeDossier(
        aerodrome=key,
        as_at=moment,
        on=day,
        sections=tuple(entries),
        notams=notams,
        unplaced=tuple(unplaced),
        cycle=cycle,
        as_known_at=as_known_at,
    )


#: Exported at package level under a name that says what it builds. ``build``
#: is fine inside ``aeropub.dossier``; at the top of the package it says
#: nothing.
build_dossier = build
