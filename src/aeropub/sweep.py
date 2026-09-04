"""The whole network at once — and the aerodromes nobody has read.

An airline with two hundred destinations does not ask *"what about OTHH"*. They
ask *"across everything I fly to, what needs attention today, and what will
need it before I next look"*. This is that question.

The dangerous artefact this module exists to avoid
--------------------------------------------------
A network dashboard showing 197 green and 3 red, where 150 of the greens are
aerodromes nobody has ever read, is the single worst thing this system could
produce. It is worse than no dashboard, because it converts absence of data
into a green tile and puts a number on it that somebody will quote in a safety
meeting.

So coverage is not a footnote here, it is a first-class column. An aerodrome
with nothing held is never counted among the clear ones: it appears in its own
section, it makes the sweep inconclusive, and :meth:`NetworkSweep.summary`
reports covered and uncovered separately so no single percentage can be quoted
without both.

What the forward half is for
----------------------------
Running :func:`aeropub.horizon.horizon` over the whole network surfaces the
thing nobody publishes: a supplement expiring at an EDTO alternate, where the
AIP figure beneath resurfaces on a date the State will never announce because,
from their side, nothing has changed. One aerodrome at a time this is
interesting; across a network it is the difference between finding out in
advance and finding out from a crew.

Everything here is assembled, not computed. The exposure comes from
:mod:`aeropub.operator`, the forward view from :mod:`aeropub.horizon`, and this
module ranks and counts. If a number here disagrees with the single-aerodrome
report for the same aerodrome, this module is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from aeropub.aip import AipCoverage
from aeropub.dossier import build
from aeropub.entities import covers
from aeropub.horizon import DEFAULT_DAYS, Horizon, Transition, horizon
from aeropub.notam_register import NotamRegister
from aeropub.operator import (
    Exposure,
    OperatorAssessment,
    OperatorProfile,
    Role,
    assess_operator,
    worst_exposure,
)

__all__ = ["AerodromeExposure", "NetworkSweep", "sweep"]


@dataclass(frozen=True, slots=True)
class AerodromeExposure:
    """One aerodrome in the network, as it stands and as it will stand."""

    aerodrome: str
    role: Role
    sole_suitable: bool
    assessment: OperatorAssessment
    facts_held: int
    """How many attributed values we hold for this aerodrome and everything on
    it. Zero is the number that matters: it means every check below rests on
    nothing, and the aerodrome must never be presented as clear."""

    ahead: Horizon | None = None

    future: tuple[tuple[date, OperatorAssessment], ...] = ()
    """The same assessment, re-run on each date a dated change takes effect.

    This is the half a forward view alone does not give. Knowing that a
    supplement expires on 21 November is interesting; knowing that the fire
    category beneath it leaves a sole-suitable EDTO alternate invalid from that
    morning, with nothing published to announce it, is the answer."""

    @property
    def exposure(self) -> Exposure:
        return self.assessment.overall

    @property
    def worsens_on(self) -> date | None:
        """The first date exposure gets worse than it is today, if any."""
        for when, assessment in self.future:
            if assessment.overall.rank < self.exposure.rank:
                return when
        return None

    @property
    def worst_ahead(self) -> Exposure:
        """The least favourable exposure across today and every date ahead."""
        return worst_exposure(
            [self.exposure] + [a.overall for _, a in self.future]
        )

    @property
    def deteriorates_unannounced(self) -> bool:
        """Whether exposure worsens on a date no State will publish about.

        The two halves have to be read together. A worsening on an announced
        AIRAC date will arrive with a publication somebody reads; a worsening
        when a supplement quietly expires arrives with nothing at all.
        """
        when = self.worsens_on
        if when is None:
            return False
        return any(t.on == when for t in self.unannounced_ahead)

    @property
    def is_covered(self) -> bool:
        """Whether anything at all has been read for this aerodrome."""
        return self.facts_held > 0

    @property
    def changes_ahead(self) -> tuple[Transition, ...]:
        return self.ahead.transitions if self.ahead else ()

    @property
    def unannounced_ahead(self) -> tuple[Transition, ...]:
        """Dated changes the State will publish nothing about.

        A supplement expiring and the AIP figure beneath it resurfacing is a
        real change to what is in force, and from the State's side nothing has
        happened.
        """
        return self.ahead.unannounced if self.ahead else ()

    @property
    def needs_action(self) -> bool:
        return self.exposure.needs_action

    def describe(self) -> str:
        only = " · sole suitable" if self.sole_suitable else ""
        coverage = "" if self.is_covered else "  [NOTHING HELD]"
        ahead = (
            f"  ·  {len(self.changes_ahead)} ahead"
            + (f", {len(self.unannounced_ahead)} unannounced" if self.unannounced_ahead else "")
            if self.changes_ahead
            else ""
        )
        worsening = (
            f"  ->  {self.worst_ahead.value} from {self.worsens_on}"
            + (" (unannounced)" if self.deteriorates_unannounced else "")
            if self.worsens_on
            else ""
        )
        return (
            f"{self.exposure.value:9} {self.aerodrome:8} "
            f"({self.role.value}{only}){coverage}{ahead}{worsening}"
        )


@dataclass(frozen=True, slots=True)
class NetworkSweep:
    """Every aerodrome this operator uses, ranked, with coverage stated."""

    operator: str
    as_at: datetime
    on: date
    entries: tuple[AerodromeExposure, ...] = ()
    days_ahead: int = DEFAULT_DAYS

    @property
    def ranked(self) -> tuple[AerodromeExposure, ...]:
        """Worst first, then uncovered, then by role, then alphabetically.

        Uncovered aerodromes sort above covered ones at the same exposure
        because an unknown at an aerodrome nobody has read is a different
        problem from an unknown at one that was read and came up short.
        """
        return tuple(
            sorted(
                self.entries,
                key=lambda e: (
                    e.exposure.rank,
                    e.is_covered,
                    not e.sole_suitable,
                    e.aerodrome,
                ),
            )
        )

    @property
    def actionable(self) -> tuple[AerodromeExposure, ...]:
        return tuple(e for e in self.ranked if e.needs_action)

    @property
    def uncovered(self) -> tuple[AerodromeExposure, ...]:
        """Aerodromes nothing has ever been read for. Never counted as clear."""
        return tuple(e for e in self.ranked if not e.is_covered)

    @property
    def overall(self) -> Exposure:
        return worst_exposure(e.exposure for e in self.entries)

    @property
    def is_conclusive(self) -> bool:
        """False while any aerodrome is unread or any assessment is unmade.

        A sweep over a network where a third of the aerodromes have never been
        read is not a conclusive statement about the network, however green the
        rest of it looks.
        """
        return bool(self.entries) and all(
            e.is_covered and e.assessment.is_conclusive for e in self.entries
        )

    @property
    def with_changes_ahead(self) -> tuple[AerodromeExposure, ...]:
        return tuple(e for e in self.ranked if e.changes_ahead)

    @property
    def with_unannounced_ahead(self) -> tuple[AerodromeExposure, ...]:
        return tuple(e for e in self.ranked if e.unannounced_ahead)

    @property
    def deteriorating(self) -> tuple[AerodromeExposure, ...]:
        """Aerodromes where exposure gets worse on a date already known.

        Soonest first. This is the list that is worth a diary entry rather than
        a dashboard: every one of these is a problem that does not exist today
        and will exist on a date that can be named now.
        """
        return tuple(
            sorted(
                (e for e in self.entries if e.worsens_on is not None),
                key=lambda e: (e.worsens_on, e.worst_ahead.rank, e.aerodrome),
            )
        )

    def summary(self) -> dict[str, int]:
        """Counts, with coverage always beside exposure.

        ``clear`` counts only aerodromes that were read *and* came back with no
        exposure. An aerodrome nobody read is in ``uncovered`` and in nothing
        else, so no single percentage can be quoted without the coverage
        number beside it.
        """
        covered = [e for e in self.entries if e.is_covered]
        return {
            "aerodromes": len(self.entries),
            "covered": len(covered),
            "uncovered": len(self.entries) - len(covered),
            "critical": sum(1 for e in covered if e.exposure is Exposure.CRITICAL),
            "high": sum(1 for e in covered if e.exposure is Exposure.HIGH),
            "medium": sum(1 for e in covered if e.exposure is Exposure.MEDIUM),
            "unknown": sum(1 for e in covered if e.exposure is Exposure.UNKNOWN),
            "clear": sum(
                1 for e in covered if e.exposure in (Exposure.NONE, Exposure.LOW)
            ),
            "changes_ahead": sum(len(e.changes_ahead) for e in self.entries),
            "unannounced_ahead": sum(len(e.unannounced_ahead) for e in self.entries),
            "deteriorating": len(self.deteriorating),
            "deteriorating_unannounced": sum(
                1 for e in self.deteriorating if e.deteriorates_unannounced
            ),
        }

    def render(self) -> str:
        counts = self.summary()
        lines = [
            f"NETWORK SWEEP — {self.operator}",
            f"as at {self.as_at:%Y-%m-%d %H:%MZ}  ·  "
            f"effective state on {self.on:%Y-%m-%d}  ·  "
            f"{self.days_ahead} days ahead",
            "",
            f"Overall: {self.overall.value.upper()}"
            + ("" if self.is_conclusive else "  ·  NOT CONCLUSIVE"),
            "",
            f"{counts['aerodromes']} aerodromes  ·  {counts['covered']} read  ·  "
            f"{counts['uncovered']} never read",
            f"{counts['critical']} critical  ·  {counts['high']} high  ·  "
            f"{counts['medium']} medium  ·  {counts['unknown']} unknown  ·  "
            f"{counts['clear']} clear",
            "",
        ]
        if not self.entries:
            lines.append(
                "No aerodromes in this network. An empty network is not a network "
                "with nothing wrong."
            )
            return "\n".join(lines)

        if counts["uncovered"]:
            lines += [
                f"!! {counts['uncovered']} of these aerodromes have never been read. "
                "They are not clear;",
                "   nothing has been checked at them, and the counts above say so "
                "separately for that reason.",
                "",
            ]

        if self.actionable:
            lines.append("NEEDS ACTION")
            for entry in self.actionable:
                lines.append(f"  {entry.describe()}")
                for finding in entry.assessment.actionable[:3]:
                    lines.append(f"      {finding.describe()}")
            lines.append("")

        if self.uncovered:
            lines.append("NOTHING HELD — read these before relying on anything above")
            lines += [
                f"  {e.aerodrome:8} ({e.role.value}"
                + (" · sole suitable" if e.sole_suitable else "")
                + ")"
                for e in self.uncovered
            ]
            lines.append("")

        if self.deteriorating:
            lines.append(
                f"EXPOSURE WORSENS AHEAD — within {self.days_ahead} days, soonest first"
            )
            for entry in self.deteriorating:
                mark = "  <- nothing will be published" if entry.deteriorates_unannounced else ""
                lines.append(
                    f"  {entry.worsens_on}  {entry.aerodrome:8} "
                    f"({entry.role.value})  {entry.exposure.value} -> "
                    f"{entry.worst_ahead.value}{mark}"
                )
                for when, assessment in entry.future:
                    if when != entry.worsens_on:
                        continue
                    for finding in assessment.actionable[:2]:
                        lines.append(f"      {finding.describe()}")
            lines.append("")

        ahead = self.with_unannounced_ahead
        if ahead:
            lines.append(
                f"CHANGING WITH NO PUBLICATION — within {self.days_ahead} days"
            )
            for entry in ahead:
                lines.append(f"  {entry.aerodrome:8} ({entry.role.value})")
                for transition in entry.unannounced_ahead[:3]:
                    lines.append(f"      {transition.describe()}")
            lines.append("")

        # An aerodrome that deteriorates ahead is not "no action" — the action
        # is to plan for it now, and it already has its own section above.
        # Listing it here as well would let a reader who scans headings stop at
        # the wrong one.
        deteriorating = {e.aerodrome for e in self.deteriorating}
        rest = [
            e
            for e in self.ranked
            if not e.needs_action
            and e.is_covered
            and e.aerodrome not in deteriorating
        ]
        if rest:
            lines.append("No action")
            lines += [f"  {e.describe()}" for e in rest]
        return "\n".join(lines)


def _facts_held(store, aerodrome: str) -> int:
    """How many attributed values we hold for this aerodrome and its objects."""
    return sum(
        len(store.attributes(entity))
        for entity in store.entities()
        if covers(aerodrome, entity)
    )


def sweep(
    store,
    profile: OperatorProfile,
    *,
    as_at: datetime | None = None,
    on: date | None = None,
    days: int = DEFAULT_DAYS,
    register: NotamRegister | None = None,
    coverage: AipCoverage | None = None,
) -> NetworkSweep:
    """Assess every aerodrome in this operator's network.

    Each aerodrome is assessed exactly as the single-aerodrome report assesses
    it, from the same dossier through the same layer three. Nothing is
    shortcut for the sweep: a number here that disagrees with the report for
    the same aerodrome is a defect in this module.
    """
    moment = as_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("as_at must be timezone-aware (UTC)")
    day = on or moment.date()

    # One entry per aerodrome, at its most demanding role — the same rule the
    # profile uses, so an aerodrome serving two roles is not swept twice under
    # two different answers.
    aerodromes = sorted({entry.aerodrome for entry in profile.network})

    entries: list[AerodromeExposure] = []
    for aerodrome in aerodromes:
        dossier = build(
            aerodrome,
            facts=store,
            coverage=coverage or AipCoverage(),
            register=register or NotamRegister(),
            as_at=moment,
            on=day,
        )
        forward = horizon(store, aerodrome, from_date=day, days=days)

        # Re-assess on each date something changes. Transitions cluster on
        # AIRAC dates, so the distinct set is far smaller than the list, and
        # re-running per date rather than per transition keeps this bounded.
        future: list[tuple[date, OperatorAssessment]] = []
        for when in sorted({t.on for t in forward.transitions}):
            future.append(
                (
                    when,
                    assess_operator(
                        build(
                            aerodrome,
                            facts=store,
                            coverage=coverage or AipCoverage(),
                            register=register or NotamRegister(),
                            as_at=moment,
                            on=when,
                        ),
                        profile,
                    ),
                )
            )

        entries.append(
            AerodromeExposure(
                aerodrome=aerodrome,
                role=profile.network.role_of(aerodrome),
                sole_suitable=profile.network.is_sole_suitable(aerodrome),
                assessment=assess_operator(dossier, profile),
                facts_held=_facts_held(store, aerodrome),
                ahead=forward,
                future=tuple(future),
            )
        )

    return NetworkSweep(
        operator=profile.name,
        as_at=moment,
        on=day,
        entries=tuple(entries),
        days_ahead=days,
    )
