"""The change bulletin — what moved between two cycles, and why it matters.

Plan section 31's milestone, in one object:

    *"Here is everything that changed at this aerodrome between AIRAC 2609 and
    2610 — every field, from what to what, why each change matters
    operationally, and exactly where every value came from."*

It composes what already exists: :func:`aeropub.changes.diff_effective`
resolves both sides through the CES, :func:`aeropub.impact.assess` reads each
difference in general terms, and :mod:`aeropub.aip` says which section of the
AIP published it. Nothing here reasons about a fleet — that is layer three,
and a bulletin is complete and useful with no operator configured.

The claim this module has to earn
---------------------------------
"Everything that changed" is a strong claim, and it is false for any section
that was not read on **both** dates. A bulletin that quietly omitted unread
sections would be the most dangerous artefact this system could produce: it
reads as a clean bill of health.

So coverage is first-class. :attr:`ChangeBulletin.blind` names the sections
where a change would not have been seen, :attr:`ChangeBulletin.is_conclusive`
is false whenever any exist, and an empty bulletin with blind sections says
*"no change detected in what was compared"* — never *"nothing changed"*. Where
no coverage is supplied at all, the bulletin says completeness cannot be
stated, rather than assuming it.

Ordering, not severity
----------------------
:class:`Attention` ranks changes for a reader with no fleet configured: a
confirmed degradation first, then anything no rule covers, then improvements,
then the rest. That is a reading order. **Severity** is a property of a change
*and* an operator — an RFFS downgrade from 9 to 7 is critical at a sole
suitable diversion for a 777 and irrelevant to an A320 operator needing
Category 6 — and it is not computed here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import IntEnum
from typing import Iterable

from aeropub.aip import SECTIONS, AipCoverage, HoldingState, Section, aerodrome_sections
from aeropub.aip import section_for_attribute
from aeropub.airac import AiracCycle
from aeropub.changes import Change, ChangeKind, diff_effective
from aeropub.entities import covers, normalise
from aeropub.facts import FactStore
from aeropub.impact import Direction, Impact, assess

__all__ = [
    "Attention",
    "ChangeBulletin",
    "ReportedChange",
    "between_cycles",
    "compile_bulletin",
]

def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """``"1 section"``, ``"2 sections"``. A bulletin is read by people."""
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


#: Publication order, so a bulletin reads down the AIP rather than alphabetically.
_SECTION_ORDER: dict[str, int] = {s.code: i for i, s in enumerate(SECTIONS)}


class Attention(IntEnum):
    """How soon a reader with no fleet configured should look at this.

    A reading order, not a severity. Severity depends on the operator and is
    computed in layer three; anything here that claimed it would be guessing
    about a fleet it has not been told about.
    """

    ACTION = 0
    """A confirmed degradation. Something got worse."""

    REVIEW = 1
    """No rule covers this attribute, so nobody has read it yet. Ranked above
    improvements because an unassessed change may be either."""

    OPPORTUNITY = 2
    """A confirmed improvement — a constraint lifted. Nothing else in this
    domain tells an operator when that happens."""

    INFORMATIONAL = 3
    """Changed, with no direction the generic layer can attach to it."""

    @property
    def label(self) -> str:
        return self.name.lower()


#: How each band is introduced. Worded so a reader knows what is being claimed:
#: the generic layer has read these, or admits it has not.
_BAND_HEADINGS: dict["Attention", str] = {}


def _attention(impact: Impact) -> Attention:
    if not impact.assessed or impact.direction is Direction.UNKNOWN:
        return Attention.REVIEW
    if impact.direction is Direction.WORSE:
        return Attention.ACTION
    if impact.direction is Direction.BETTER:
        return Attention.OPPORTUNITY
    return Attention.INFORMATIONAL


_BAND_HEADINGS.update(
    {
        Attention.ACTION: "ACTION — something got worse",
        Attention.REVIEW: "NEEDS A HUMAN — no rule covers these, and they may be either",
        Attention.OPPORTUNITY: "OPPORTUNITY — a constraint lifted",
        Attention.INFORMATIONAL: "INFORMATIONAL — changed, with no direction attached",
    }
)


@dataclass(frozen=True, slots=True)
class ReportedChange:
    """One change, read generically, placed in the AIP."""

    impact: Impact
    section: Section | None
    """Where the AIP publishes this attribute, or ``None`` where we have not
    mapped it. Shown as unplaced rather than filed under a guess."""

    attention: Attention

    @property
    def change(self) -> Change:
        return self.impact.change

    @property
    def entity(self) -> str:
        return self.change.entity

    @property
    def attribute(self) -> str:
        return self.change.attribute

    @property
    def kind(self) -> ChangeKind:
        return self.change.kind

    @property
    def domains(self) -> tuple[str, ...]:
        """Domains from the impact rule, falling back to the section's.

        An unassessed change still belongs to the section it was published in,
        so it can still reach the people who read that section — otherwise
        "no rule covers this" would also mean "nobody hears about it".
        """
        return self.impact.domains or (self.section.domains if self.section else ())

    @property
    def sort_key(self) -> tuple:
        placed = _SECTION_ORDER.get(self.section.code, len(SECTIONS)) if self.section else len(SECTIONS)
        return (int(self.attention), placed, self.entity, self.attribute)

    def describe(self) -> str:
        where = self.section.code if self.section else "unplaced"
        return f"[{self.attention.label}] {where} · {self.impact.summary}"

    def citations(self) -> tuple[str, ...]:
        """Both sides, so a reader can check the change rather than trust it."""
        out = []
        if self.change.before is not None:
            out.append(f"was: {self.change.before.source.describe()}")
        if self.change.after is not None:
            out.append(f"now: {self.change.after.source.describe()}")
        return tuple(out)


@dataclass(frozen=True, slots=True)
class ChangeBulletin:
    """Everything that changed for one entity between two dates."""

    entity: str
    before: date
    after: date
    changes: tuple[ReportedChange, ...]

    covered: tuple[Section, ...] = ()
    """Sections held on both dates — the only ones this bulletin speaks for."""

    blind: tuple[Section, ...] = ()
    """Sections not held on one or both dates. A change in these would not
    have been seen, and the bulletin says so rather than implying otherwise."""

    coverage_known: bool = False
    """Whether coverage was supplied at all. Without it, completeness cannot
    be stated — and is not."""

    before_cycle: AiracCycle | None = None
    after_cycle: AiracCycle | None = None

    # -- views -----------------------------------------------------------

    @property
    def is_conclusive(self) -> bool:
        """Whether "everything that changed" is a claim this bulletin can make."""
        return self.coverage_known and not self.blind

    def at(self, attention: Attention) -> tuple[ReportedChange, ...]:
        return tuple(c for c in self.changes if c.attention is attention)

    @property
    def action(self) -> tuple[ReportedChange, ...]:
        return self.at(Attention.ACTION)

    @property
    def needs_human(self) -> tuple[ReportedChange, ...]:
        return self.at(Attention.REVIEW)

    @property
    def opportunities(self) -> tuple[ReportedChange, ...]:
        return self.at(Attention.OPPORTUNITY)

    def for_domain(self, domain: str) -> tuple[ReportedChange, ...]:
        """The lens: everything a dispatcher, or a chart team, needs to see."""
        return tuple(c for c in self.changes if domain in c.domains)

    def by_section(self) -> tuple[tuple[Section | None, tuple[ReportedChange, ...]], ...]:
        """Grouped in publication order, with unplaced changes last."""
        grouped: dict[str | None, list[ReportedChange]] = defaultdict(list)
        sections: dict[str | None, Section | None] = {}
        for reported in self.changes:
            code = reported.section.code if reported.section else None
            grouped[code].append(reported)
            sections[code] = reported.section
        ordered = sorted(
            grouped,
            key=lambda code: _SECTION_ORDER.get(code, len(SECTIONS)) if code else len(SECTIONS) + 1,
        )
        return tuple((sections[code], tuple(grouped[code])) for code in ordered)

    def summary(self) -> dict[str, int | bool]:
        return {
            "changes": len(self.changes),
            "action": len(self.action),
            "review": len(self.needs_human),
            "opportunity": len(self.opportunities),
            "informational": len(self.at(Attention.INFORMATIONAL)),
            "sections_compared": len(self.covered),
            "sections_blind": len(self.blind),
            "conclusive": self.is_conclusive,
        }

    # -- output ----------------------------------------------------------

    def _period(self) -> str:
        if self.before_cycle and self.after_cycle:
            return (
                f"AIRAC {self.before_cycle.identifier} → "
                f"{self.after_cycle.identifier}"
                f"  ({self.before:%Y-%m-%d} → {self.after:%Y-%m-%d})"
            )
        return f"{self.before:%Y-%m-%d} → {self.after:%Y-%m-%d}"

    def coverage_statement(self) -> str:
        """What this bulletin can honestly claim, in one sentence."""
        if not self.coverage_known:
            return (
                "Coverage was not supplied, so this bulletin cannot state whether "
                "it is complete. Treat it as a list of detected changes, not as "
                "the absence of others."
            )
        if not self.blind:
            return (
                f"Complete for AD 2: all {_plural(len(self.covered), 'section')} were "
                "held on both dates, so a change in any of them would have been "
                "detected."
            )
        was = "was" if len(self.blind) == 1 else "were"
        return (
            f"NOT complete. {_plural(len(self.covered), 'section')} compared; "
            f"{_plural(len(self.blind), 'section')} {was} not held on both dates, and "
            "a change there would not appear below."
        )

    def render(self) -> str:
        counts = self.summary()
        lines = [
            f"CHANGE BULLETIN — {self.entity}",
            self._period(),
            "",
            self.coverage_statement(),
            "",
        ]

        if not self.changes:
            lines.append(
                "No change detected in what was compared."
                if not self.is_conclusive
                else "No change. Every compared section is as it was."
            )
        else:
            needs = "needs" if counts["review"] == 1 else "need"
            lines.append(
                f"{_plural(counts['changes'], 'change')}  ·  "
                f"{counts['action']} action  ·  "
                f"{counts['review']} {needs} a human  ·  "
                f"{_plural(counts['opportunity'], 'opportunity', 'opportunities')}"
            )
            # Banded by attention, and within a band by publication order. A
            # bulletin is read for what to do about it, so a degradation must
            # not sit below an advisory because AD 2.6 precedes AD 2.14. The
            # dossier is the artefact that reads down the AIP; by_section()
            # gives the same content in that order for a reader who wants it.
            for band in Attention:
                items = self.at(band)
                if not items:
                    continue
                lines += ["", _BAND_HEADINGS[band]]
                for item in items:
                    where = item.section.code if item.section else "unplaced"
                    lines.append(f"  {where:9} {item.impact.summary}")
                    lines.append(f"      {item.impact.consequence}")
                    for citation in item.citations():
                        lines.append(f"      {citation}")

        if self.blind:
            lines += [
                "",
                "NOT COMPARED — a change in these sections would not appear above",
            ]
            lines += [f"  {s.code:9} {s.title}" for s in self.blind]
        return "\n".join(lines)


def _held_on_both(
    entity: str,
    coverage_before: AipCoverage | None,
    coverage_after: AipCoverage | None,
) -> tuple[tuple[Section, ...], tuple[Section, ...]]:
    """Split AD 2 into what was comparable and what was not."""
    if coverage_before is None and coverage_after is None:
        return (), ()
    covered, blind = [], []
    for candidate in aerodrome_sections():
        states = [
            (cov.holding(entity, candidate.code).state if cov is not None else None)
            for cov in (coverage_before, coverage_after)
        ]
        # A section missing from one side is not comparable, whatever the other
        # side says. ABSENT counts as comparable: a State that publishes no
        # helicopter area on both dates has not changed it.
        if all(s in (HoldingState.HELD, HoldingState.ABSENT) for s in states):
            covered.append(candidate)
        else:
            blind.append(candidate)
    return tuple(covered), tuple(blind)


def compile_bulletin(
    store: FactStore,
    entity: str,
    before: date,
    after: date,
    *,
    coverage_before: AipCoverage | None = None,
    coverage_after: AipCoverage | None = None,
    before_cycle: AiracCycle | None = None,
    after_cycle: AiracCycle | None = None,
    attributes: Iterable[str] | None = None,
) -> ChangeBulletin:
    """Compile the bulletin for one entity between two dates.

    Both sides are resolved through the CES, so a supplement expiring registers
    as a change even though nothing was published to cause it — which is
    exactly the change a document diff would miss.

    Facts on anything beneath ``entity`` are included: a runway's declared
    distances belong in the aerodrome's bulletin.
    """
    key = normalise(entity)
    if not key:
        raise ValueError("entity must be a non-empty string")
    if after < before:
        raise ValueError(f"after ({after}) precedes before ({before})")

    wanted = set(attributes) if attributes is not None else None
    reported: list[ReportedChange] = []
    for candidate in sorted(store.entities()):
        if not covers(key, candidate):
            continue
        for change in diff_effective(
            store, before, after, entity=candidate, attributes=wanted
        ):
            impact = assess(change)
            reported.append(
                ReportedChange(
                    impact=impact,
                    section=section_for_attribute(change.attribute),
                    attention=_attention(impact),
                )
            )

    reported.sort(key=lambda r: r.sort_key)
    covered, blind = _held_on_both(key, coverage_before, coverage_after)

    return ChangeBulletin(
        entity=key,
        before=before,
        after=after,
        changes=tuple(reported),
        covered=covered,
        blind=blind,
        coverage_known=not (coverage_before is None and coverage_after is None),
        before_cycle=before_cycle,
        after_cycle=after_cycle,
    )


def between_cycles(
    store: FactStore,
    entity: str,
    before: AiracCycle,
    after: AiracCycle,
    **kwargs,
) -> ChangeBulletin:
    """The bulletin between two AIRAC cycles, each sampled on its effective date."""
    return compile_bulletin(
        store,
        entity,
        before.effective_date,
        after.effective_date,
        before_cycle=before,
        after_cycle=after,
        **kwargs,
    )
