"""Findings about how a State publishes, not about what it published.

Everything else here reads publications for their content. This module reads
them for their *conduct*, and the difference matters operationally rather than
academically.

ICAO PANS-AIM (Doc 10066) and Annex 15 are explicit: a NOTAM is for temporary,
short-notice or short-duration information, and a condition expected to persist
should move to an AIP Supplement or an AIP Amendment. Three months is the
stated limit. When a State leaves a condition on NOTAM past that, three things
follow, and none of them are theoretical:

- **It will be missed.** A permanent runway restriction living in a NOTAM
  bulletin gets read once and skimmed thereafter. In the AIP it is in the
  section a person opens when they plan.
- **It does not reach the people who need it.** Aerodrome studies, alternate
  categorisations and payload tables are built from the AIP. A condition that
  never lands there never reaches the document that decides where an aircraft
  can go.
- **It is a signal about the State.** Somewhere with a standing habit of this
  is somewhere to weight more carefully when choosing an unfamiliar alternate.

The serial re-issue is the harder case and the more common one. Each individual
NOTAM sits inside the limit; the *condition* has run for eleven months across
nine of them. Reading messages one at a time cannot see it. Holding them all,
against one register, can.

Nothing here calls a State non-compliant. It reports the duration, cites the
messages, and names the standard, and lets a reader who knows the context draw
the conclusion. A quality harness that cries wolf gets switched off, and then
the real findings go with it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable

from aeropub.airac import AiracCycle
from aeropub.changes import diff_effective
from aeropub.entities import covers, normalise
from aeropub.notam_register import ForceState, NotamRegister, RegisteredNotam
from aeropub.provenance import SourceRef

__all__ = [
    "MAX_NOTAM_DAYS",
    "FindingKind",
    "QualityFinding",
    "QualityReport",
    "assess_quality",
    "permanent_by_notam",
    "serial_reissues",
    "volatility",
]

#: PANS-AIM: a NOTAM is not the place for information expected to persist
#: beyond three months. Past this, the condition belongs in a Supplement or an
#: Amendment. Ninety days rather than three calendar months because a duration
#: comparison needs a number, and the difference is never the point.
MAX_NOTAM_DAYS = 90

#: How many cycles of history a volatility reading is taken over. Six is about
#: half a year — long enough that one disruptive works programme does not
#: define an aerodrome, short enough to still describe it now.
VOLATILITY_CYCLES = 6

#: Changes per cycle above which an aerodrome is worth calling unstable. Not a
#: standard — a reading aid, and labelled as one wherever it is rendered.
VOLATILITY_THRESHOLD = 3.0


class FindingKind(str, Enum):
    """What kind of publication conduct was observed."""

    PERMANENT_BY_NOTAM = "permanent_by_notam"
    """One NOTAM has carried a condition past the three-month limit."""

    SERIAL_REISSUE = "serial_reissue"
    """Successive NOTAM have carried one condition past the limit between
    them, each individually within it. The one that reading messages
    separately cannot find."""

    LAPSED_ESTIMATE = "lapsed_estimate"
    """A NOTAM with an estimated end whose estimate has passed and which was
    never updated. Its stated end date is now fiction."""

    VOLATILE = "volatile"
    """An aerodrome changing far more often than its neighbours."""

    @property
    def concerns_notam_practice(self) -> bool:
        return self in (
            FindingKind.PERMANENT_BY_NOTAM,
            FindingKind.SERIAL_REISSUE,
            FindingKind.LAPSED_ESTIMATE,
        )


@dataclass(frozen=True, slots=True)
class QualityFinding:
    """One observation about publication conduct, with its evidence."""

    kind: FindingKind
    entity: str
    summary: str
    """What was observed. A measurement, not a verdict."""

    consequence: str
    """Why it matters operationally."""

    days: int | None = None
    messages: tuple[str, ...] = ()
    """Identifiers of the NOTAM this rests on, so it can be checked."""

    sources: tuple[SourceRef, ...] = ()
    detail: str = ""

    @property
    def sort_key(self) -> tuple:
        return (self.kind.value, -(self.days or 0), self.entity)

    def describe(self) -> str:
        span = f" ({self.days} days)" if self.days is not None else ""
        return f"[{self.kind.value}] {self.entity}: {self.summary}{span}"

    def citations(self) -> tuple[str, ...]:
        return tuple(s.describe() for s in self.sources)


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Publication conduct observed for one entity or authority."""

    scope: str
    as_at: datetime
    findings: tuple[QualityFinding, ...] = ()
    notams_examined: int = 0
    changes_examined: int = 0
    cycles_examined: int = 0

    def of_kind(self, kind: FindingKind) -> tuple[QualityFinding, ...]:
        return tuple(f for f in self.findings if f.kind is kind)

    @property
    def notam_practice(self) -> tuple[QualityFinding, ...]:
        return tuple(f for f in self.findings if f.kind.concerns_notam_practice)

    def summary(self) -> dict[str, int]:
        counts = {kind.value: len(self.of_kind(kind)) for kind in FindingKind}
        counts["findings"] = len(self.findings)
        counts["notams_examined"] = self.notams_examined
        return counts

    def render(self) -> str:
        lines = [
            f"PUBLICATION CONDUCT — {self.scope}",
            f"as at {self.as_at:%Y-%m-%d %H:%MZ}  ·  "
            f"{self.notams_examined} NOTAM examined"
            + (f"  ·  {self.cycles_examined} cycles" if self.cycles_examined else ""),
            "",
        ]
        if not self.findings:
            lines.append(
                "Nothing observed. That is a statement about what was examined, "
                "not a clean bill of health for what was not."
            )
            return "\n".join(lines)

        lines.append(f"{len(self.findings)} findings")
        for finding in self.findings:
            lines += ["", f"  {finding.describe()}", f"      {finding.consequence}"]
            if finding.detail:
                lines.append(f"      {finding.detail}")
            if finding.messages:
                lines.append(f"      messages: {', '.join(finding.messages)}")
            for citation in finding.citations()[:3]:
                lines.append(f"      {citation}")
            if len(finding.sources) > 3:
                lines.append(f"      … and {len(finding.sources) - 3} more")

        lines += [
            "",
            "PANS-AIM: information expected to persist beyond three months "
            "belongs in an AIP Supplement or Amendment, not a NOTAM. These are "
            "measurements against that, not a compliance judgement.",
        ]
        return "\n".join(lines)


def _elapsed_days(notam: RegisteredNotam, as_at: datetime) -> int | None:
    """How long this message has been in force by now."""
    if notam.effective_start is None or notam.effective_start > as_at:
        return None
    end = min(notam.effective_end, as_at) if notam.effective_end else as_at
    return max((end - notam.effective_start).days, 0)


def _condition_key(notam: RegisteredNotam) -> tuple[str, str]:
    """What makes two messages the same condition.

    Same objects, same words. Deliberately strict: a looser match would group
    unrelated work at one aerodrome and produce a finding nobody can check,
    and a quality harness that cries wolf gets switched off.
    """
    return (
        "|".join(sorted(notam.entities)),
        " ".join(notam.text.upper().split()),
    )


def permanent_by_notam(
    register: NotamRegister,
    as_at: datetime,
    *,
    entity: str | None = None,
    threshold_days: int = MAX_NOTAM_DAYS,
) -> tuple[QualityFinding, ...]:
    """Single NOTAM that have carried a condition past the three-month limit.

    Only messages still in force. One that ended last month is no longer
    carrying anything, and reporting it would fill an operational list with
    history. :func:`serial_reissues` deliberately does the opposite and spans
    expired messages, because there the *condition* is what is measured and the
    earlier messages are the evidence of how long it has run.
    """
    findings = []
    for notam in register:
        if entity is not None and not notam.affects(entity):
            continue
        days = _elapsed_days(notam, as_at)
        if days is None or days <= threshold_days:
            continue
        if notam.state_at(as_at) is ForceState.EXPIRED:
            continue
        subject = notam.entities[0] if notam.entities else "unattributed"
        findings.append(
            QualityFinding(
                kind=FindingKind.PERMANENT_BY_NOTAM,
                entity=subject,
                summary=f"{notam.identifier} has been in force for {days} days",
                consequence=(
                    "A condition this long-lived belongs in an AIP Supplement or "
                    "Amendment, where planning documents will pick it up. Left on "
                    "NOTAM it reaches a bulletin that is skimmed, and never reaches "
                    "the aerodrome study or the payload table."
                ),
                days=days,
                messages=(notam.identifier,),
                sources=(notam.source,),
                detail=notam.text[:160] if notam.text else "",
            )
        )
    return tuple(findings)


def serial_reissues(
    register: NotamRegister,
    as_at: datetime,
    *,
    entity: str | None = None,
    threshold_days: int = MAX_NOTAM_DAYS,
    minimum_messages: int = 2,
) -> tuple[QualityFinding, ...]:
    """One condition carried past the limit by successive NOTAM.

    Each message may sit comfortably inside three months while the condition
    has run for a year across nine of them. Reading messages one at a time
    cannot see this; a register holding all of them can.
    """
    groups: dict[tuple[str, str], list[RegisteredNotam]] = defaultdict(list)
    for notam in register:
        if entity is not None and not notam.affects(entity):
            continue
        if not notam.text.strip() or notam.effective_start is None:
            continue
        groups[_condition_key(notam)].append(notam)

    findings = []
    for (subjects, _), messages in groups.items():
        if len(messages) < minimum_messages:
            continue
        ordered = sorted(messages, key=lambda n: n.effective_start)
        first = ordered[0].effective_start
        ends = [min(n.effective_end, as_at) if n.effective_end else as_at
                for n in ordered]
        span = max((max(ends) - first).days, 0)
        if span <= threshold_days:
            continue
        findings.append(
            QualityFinding(
                kind=FindingKind.SERIAL_REISSUE,
                entity=subjects.split("|")[0],
                summary=(
                    f"one condition carried by {len(ordered)} successive NOTAM "
                    f"since {first:%Y-%m-%d}"
                ),
                consequence=(
                    "Each message is within the three-month limit; the condition "
                    "is not. Nothing in a NOTAM bulletin shows the cumulative "
                    "duration, so this is invisible to anyone reading messages as "
                    "they arrive — which is everyone."
                ),
                days=span,
                messages=tuple(n.identifier for n in ordered),
                sources=tuple(n.source for n in ordered),
                detail=ordered[0].text[:160],
            )
        )
    return tuple(findings)


def lapsed_estimates(
    register: NotamRegister, as_at: datetime, *, entity: str | None = None
) -> tuple[QualityFinding, ...]:
    """NOTAM whose estimated end has passed without being updated.

    An estimated end is a promise to revise. Once it is behind us the message
    is still notionally in force with a date that is now fiction, and the only
    honest reading of its remaining duration is that nobody knows.
    """
    findings = []
    for notam in register:
        if entity is not None and not notam.affects(entity):
            continue
        if not notam.estimated or notam.effective_end is None:
            continue
        if notam.effective_end >= as_at:
            continue
        overdue = (as_at - notam.effective_end).days
        subject = notam.entities[0] if notam.entities else "unattributed"
        findings.append(
            QualityFinding(
                kind=FindingKind.LAPSED_ESTIMATE,
                entity=subject,
                summary=(
                    f"{notam.identifier} estimated an end on "
                    f"{notam.effective_end:%Y-%m-%d} and was not revised"
                ),
                consequence=(
                    "The stated end date is no longer meaningful. Treat the "
                    "condition as of unknown duration rather than about to lift, "
                    "and do not plan a return to the underlying value against it."
                ),
                days=overdue,
                messages=(notam.identifier,),
                sources=(notam.source,),
                detail=notam.text[:160] if notam.text else "",
            )
        )
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class Volatility:
    """How often an aerodrome's effective state moved, cycle over cycle."""

    entity: str
    cycles: tuple[AiracCycle, ...]
    per_cycle: tuple[int, ...]
    """Changes between each cycle and the one before it."""

    @property
    def total(self) -> int:
        return sum(self.per_cycle)

    @property
    def rate(self) -> float:
        return self.total / len(self.per_cycle) if self.per_cycle else 0.0

    @property
    def is_unstable(self) -> bool:
        return self.rate >= VOLATILITY_THRESHOLD

    def describe(self) -> str:
        window = (
            f"{self.cycles[0].identifier}–{self.cycles[-1].identifier}"
            if self.cycles else "no cycles"
        )
        return (
            f"{self.total} changes across {len(self.per_cycle)} cycles "
            f"({window}), {self.rate:.1f} per cycle"
        )


def volatility(
    store,
    entity: str,
    *,
    through: AiracCycle,
    cycles: int = VOLATILITY_CYCLES,
) -> Volatility:
    """How much an aerodrome has moved over the last few cycles.

    A reading aid for choosing between alternates, not a standard. An aerodrome
    changing three times a cycle is one to look at before relying on a dossier
    signed against it a month ago.
    """
    key = normalise(entity)
    window = [through.shifted_by(-n) for n in range(cycles, -1, -1)]
    counts = []
    for earlier, later in zip(window, window[1:]):
        moved = 0
        for candidate in sorted(store.entities()):
            if not covers(key, candidate):
                continue
            moved += len(
                diff_effective(
                    store, earlier.effective_date, later.effective_date,
                    entity=candidate,
                )
            )
        counts.append(moved)
    return Volatility(entity=key, cycles=tuple(window[1:]), per_cycle=tuple(counts))


def assess_quality(
    *,
    register: NotamRegister | None = None,
    store=None,
    entity: str | None = None,
    as_at: datetime | None = None,
    through: AiracCycle | None = None,
    cycles: int = VOLATILITY_CYCLES,
) -> QualityReport:
    """Every publication-conduct finding available from what is supplied.

    Each input is optional and each omission narrows the report rather than
    failing it — but the report says what it examined, so a short list is never
    mistaken for a clean one.
    """
    moment = as_at or datetime.now(tz=datetime.now().astimezone().tzinfo)
    if moment.tzinfo is None:
        raise ValueError("as_at must be timezone-aware (UTC)")
    scope = normalise(entity) if entity else "all indexed entities"

    findings: list[QualityFinding] = []
    examined = 0
    if register is not None:
        examined = len(register)
        findings += permanent_by_notam(register, moment, entity=entity)
        findings += serial_reissues(register, moment, entity=entity)
        findings += lapsed_estimates(register, moment, entity=entity)

    cycles_examined = 0
    if store is not None and entity is not None and through is not None:
        reading = volatility(store, entity, through=through, cycles=cycles)
        cycles_examined = len(reading.per_cycle)
        if reading.is_unstable:
            findings.append(
                QualityFinding(
                    kind=FindingKind.VOLATILE,
                    entity=reading.entity,
                    summary=reading.describe(),
                    consequence=(
                        "An aerodrome moving this often outdates a signed study "
                        "quickly. Re-read it before relying on a categorisation "
                        "made more than a cycle ago, and prefer a steadier field "
                        "where an alternate choice is otherwise even."
                    ),
                )
            )

    findings.sort(key=lambda f: f.sort_key)
    return QualityReport(
        scope=scope,
        as_at=moment,
        findings=tuple(findings),
        notams_examined=examined,
        cycles_examined=cycles_examined,
    )
