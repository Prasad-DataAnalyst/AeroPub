"""What was knowable, and when — as distinct from what turned out to be true.

The plan's highest-value roadmap item, and it needed almost no new machinery:
every :class:`~aeropub.facts.Fact` already carries ``valid_from`` and
``recorded_at``, the archive is never pruned, and ``effective()`` already takes
``as_known_at``. What was missing was the question.

The distinction everything here exists to protect
-------------------------------------------------
**"What was in force on 15 October" and "what anybody could have known on 15
October" are different documents.** The first is the corrected record — today's
holdings, filtered to that day's validity, including a NOTAM that reached us
three days late. The second is what the platform could actually have printed
that morning.

Every system that offers a date picker returns the first and calls it history.
For a safety investigation the second is the only honest answer, because
reporting the corrected record as though it were contemporaneous quietly blames
a crew for not knowing something nobody had sent them yet.

So this module never returns one number. It returns **both, and the gap between
them**, and a :class:`Retrospect` where the two agree says so explicitly rather
than staying silent.

Blindness, measured
-------------------
The measurement that falls out is the one nobody publishes: how long a change
was operationally in force before the platform held it. A NOTAM effective from
11 October at 1420Z that we recorded on 14 October at 0900Z left a **66-hour
blind window** during which every dossier for that aerodrome was confidently
wrong. That number is an auditable fact about our own collection, it aggregates
into a per-source measure of how well we are actually watching, and it is
computed from data already in the store rather than from any bookkeeping that
could drift.

It is deliberately about *us*, not about the State. A State that publishes late
is :mod:`aeropub.quality`'s subject. This is the mirror: what we were late to
read.

What this cannot do, stated plainly
-----------------------------------
The NOTAM register is not bitemporal — it records when a NOTAM is *effective*,
not when we learned of it. So a retrospective dossier's NOTAM section is not
retrospective, and :meth:`Retrospect.render` says so rather than presenting a
mixed document as a clean one. Making the register bitemporal is a real piece
of work and it is not pretended here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from aeropub.entities import covers
from aeropub.facts import Fact

__all__ = [
    "Blindness",
    "LateArrival",
    "Retrospect",
    "Revision",
    "blind_spots",
    "retrospect",
]


def _midnight(day: date) -> datetime:
    """The instant a validity date begins, in UTC.

    A ``valid_from`` is a date and a ``recorded_at`` is an instant, so measuring
    between them needs one convention. Taking the start of the day is the
    conservative one: it makes a blind window as long as it could have been,
    which is the right direction for a measure of our own lateness.
    """
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class LateArrival:
    """A change that took effect while we were watching, before we held it.

    ``watching_since`` is what stops this measuring the wrong thing. A standing
    AIP value effective since January, first read when the entity was onboarded
    in September, is not eight months of blindness — we were not watching, and
    calling that a blind window makes every new source look catastrophic while
    burying the case that matters. Only a change that took effect *after* we
    started watching can be one we were late to.
    """

    fact: Fact
    watching_since: datetime

    @property
    def effective_from(self) -> date:
        return self.fact.valid_from

    @property
    def known_from(self) -> datetime:
        return self.fact.recorded_at

    @property
    def predates_watching(self) -> bool:
        """Whether it was already in force before we watched this entity at all."""
        return _midnight(self.effective_from) < self.watching_since

    @property
    def blind(self) -> timedelta:
        """How long it was operationally in force before we held it.

        Zero in the two cases that are not blindness: a fact recorded before it
        takes effect — the healthy case, an AIRAC amendment held 42 days ahead
        — and a value that predates our watching this entity at all.
        """
        if self.predates_watching:
            return timedelta(0)
        gap = self.known_from - _midnight(self.effective_from)
        return gap if gap > timedelta(0) else timedelta(0)

    @property
    def blind_hours(self) -> float:
        return round(self.blind.total_seconds() / 3600.0, 1)

    @property
    def was_blind(self) -> bool:
        return self.blind > timedelta(0)

    def describe(self) -> str:
        return (
            f"{self.fact.entity} {self.fact.attribute} = {self.fact.value} "
            f"— in force from {self.effective_from}, held from "
            f"{self.known_from:%Y-%m-%d %H:%MZ} "
            f"({self.blind_hours:g}h blind)"
        )


@dataclass(frozen=True, slots=True)
class Blindness:
    """How late our collection was, over a set of facts.

    ``worst`` matters more than ``mean``: a collection that is usually prompt
    and occasionally three days late is not the same as one that is uniformly
    ninety minutes late, and averaging them together hides exactly the case an
    investigation is looking for.
    """

    arrivals: tuple[LateArrival, ...] = ()

    @property
    def late(self) -> tuple[LateArrival, ...]:
        return tuple(a for a in self.arrivals if a.was_blind)

    @property
    def worst(self) -> LateArrival | None:
        return max(self.late, key=lambda a: a.blind, default=None)

    @property
    def total_blind_hours(self) -> float:
        return round(sum(a.blind_hours for a in self.late), 1)

    @property
    def mean_blind_hours(self) -> float:
        found = self.late
        return round(sum(a.blind_hours for a in found) / len(found), 1) if found else 0.0

    def summary(self) -> dict[str, float | int]:
        return {
            "facts": len(self.arrivals),
            "late": len(self.late),
            "worst_hours": self.worst.blind_hours if self.worst else 0.0,
            "mean_hours": self.mean_blind_hours,
            "total_hours": self.total_blind_hours,
        }


@dataclass(frozen=True, slots=True)
class Revision:
    """One attribute, as we saw it then and as we see it now."""

    entity: str
    attribute: str
    then: Fact | None
    now: Fact | None

    @property
    def appeared(self) -> bool:
        """We hold a value now and held none then."""
        return self.then is None and self.now is not None

    @property
    def withdrawn(self) -> bool:
        """We held a value then and hold none now — a correction, or an expiry
        we have since learned about."""
        return self.then is not None and self.now is None

    @property
    def restated(self) -> bool:
        """The value differs between the two readings."""
        return (
            self.then is not None
            and self.now is not None
            and self.then.value != self.now.value
        )

    @property
    def changed(self) -> bool:
        return self.appeared or self.withdrawn or self.restated

    @property
    def is_held(self) -> bool:
        """Whether either reading had a value for this attribute on that day.

        ``False`` means the attribute has no value on that date in either
        reading — usually because the fact's validity had not begun. That is
        not agreement between the two readings and must not be counted as
        though it were: "4 of 5 attributes read the same" implies four were
        examined and matched, when four had nothing to compare.
        """
        return self.then is not None or self.now is not None

    def describe(self) -> str:
        if self.appeared:
            return (
                f"{self.attribute}: nothing held then; {self.now.value} now "
                f"(from {self.now.source.document})"
            )
        if self.withdrawn:
            return f"{self.attribute}: {self.then.value} then; nothing held now"
        if self.restated:
            return (
                f"{self.attribute}: {self.then.value} then; {self.now.value} now "
                f"(from {self.now.source.document})"
            )
        value = self.now.value if self.now else None
        return f"{self.attribute}: {value}, unchanged"


@dataclass(frozen=True, slots=True)
class Retrospect:
    """One entity, one day, seen twice: as it was known then and as it is now."""

    entity: str
    on: date
    as_known_at: datetime
    revisions: tuple[Revision, ...] = ()
    blindness: Blindness = Blindness()
    notam_is_retrospective: bool = False
    """Whether the NOTAM picture is also filtered to that moment's knowledge.

    ``False``, because the register is not bitemporal. Carried as a field so
    the limitation travels with the document rather than living only in a
    docstring nobody reads at the time."""

    @property
    def changed(self) -> tuple[Revision, ...]:
        return tuple(r for r in self.revisions if r.changed)

    @property
    def compared(self) -> tuple[Revision, ...]:
        """Attributes that had a value in at least one reading.

        The honest denominator. Attributes with nothing on either side were not
        compared and are excluded rather than counted as agreement.
        """
        return tuple(r for r in self.revisions if r.is_held)

    @property
    def is_faithful(self) -> bool:
        """Whether what we would have said then is what we would say now.

        ``True`` is a real and useful answer — it means the record has not moved
        under this date, which is what an audit wants to hear. It is stated
        rather than left to be inferred from an empty list.
        """
        return not self.changed

    def summary(self) -> dict[str, int]:
        return {
            "attributes": len(self.revisions),
            "compared": len(self.compared),
            "not_in_force": len(self.revisions) - len(self.compared),
            "changed": len(self.changed),
            "appeared": sum(1 for r in self.revisions if r.appeared),
            "withdrawn": sum(1 for r in self.revisions if r.withdrawn),
            "restated": sum(1 for r in self.revisions if r.restated),
            "late": len(self.blindness.late),
        }

    def render(self) -> str:
        counts = self.summary()
        lines = [
            f"RETROSPECT — {self.entity} on {self.on:%Y-%m-%d}",
            f"as known at {self.as_known_at:%Y-%m-%d %H:%MZ}, compared with what "
            "is held now",
            "",
        ]
        if self.is_faithful:
            lines.append(
                f"FAITHFUL — all {counts['compared']} attributes in force that "
                "day read the same then as now."
            )
            lines.append(
                "What the platform would have said that day is what it says today."
            )
        else:
            lines.append(
                f"{counts['changed']} of {counts['compared']} attributes in force "
                "that day read differently then."
            )
            lines.append(
                "A report produced at that moment would not have matched today's."
            )
            lines.append("")
            lines.append("WHAT MOVED")
            lines += [f"  {r.describe()}" for r in self.changed]

        if counts["not_in_force"]:
            lines += [
                "",
                f"{counts['not_in_force']} further attribute"
                f"{'s' if counts['not_in_force'] != 1 else ''} held for this "
                "entity had no value in force on that",
                "day in either reading, and were not compared.",
            ]

        if self.blindness.late:
            worst = self.blindness.worst
            lines += [
                "",
                f"ARRIVED LATE — {len(self.blindness.late)} value"
                f"{'s' if len(self.blindness.late) != 1 else ''} were already in "
                "force when we first held them",
            ]
            lines += [f"  {a.describe()}" for a in self.blindness.late]
            lines.append(
                f"  Worst blind window: {worst.blind_hours:g} hours. Every "
                "dossier for this entity in that"
            )
            lines.append(
                "  window was confidently wrong, and said nothing to suggest it."
            )

        if not self.notam_is_retrospective:
            lines += [
                "",
                "!! NOTAM are NOT included in this retrospective view. The register "
                "records when a",
                "   NOTAM is effective, not when we learned of it, so it cannot be "
                "filtered to what",
                "   was known at a past moment. Read the NOTAM picture as current, "
                "not as of then.",
            ]
        return "\n".join(lines)


def _facts_for(store, entity: str) -> list[Fact]:
    return [
        fact
        for held in store.entities()
        if covers(entity, held)
        for attribute in store.attributes(held)
        for fact in store.history(held, attribute)
    ]


def blind_spots(
    store, entity: str, *, through: datetime | None = None
) -> Blindness:
    """How late our collection was for everything held about this entity.

    A measure of us, not of the State. Two things contribute nothing, and
    excluding them is what makes the number mean anything:

    - a fact recorded *before* it takes effect — the healthy case, an AIRAC
      amendment held 42 days ahead;
    - a value that was already in force before we started watching this entity
      at all, which is onboarding rather than lateness.

    What is left is the real question: a change took effect while we were
    watching, and for some window we did not have it.
    """
    limit = through or datetime.now(timezone.utc)
    held = [f for f in _facts_for(store, entity) if f.recorded_at <= limit]
    if not held:
        return Blindness()
    watching_since = min(f.recorded_at for f in held)
    return Blindness(
        arrivals=tuple(
            LateArrival(fact=fact, watching_since=watching_since) for fact in held
        )
    )


def retrospect(
    store,
    entity: str,
    *,
    on: date,
    as_known_at: datetime,
) -> Retrospect:
    """Compare what was knowable at a moment with what is held now.

    ``on`` is the day whose effective state is in question; ``as_known_at`` is
    the moment whose knowledge to use. Both are required — defaulting either
    would let a caller ask the ambiguous question this module exists to
    separate.
    """
    if as_known_at.tzinfo is None:
        raise ValueError(
            "as_known_at must be timezone-aware (UTC). A naive instant cannot "
            "be compared against a recorded_at, and getting this wrong by a "
            "timezone is exactly how a retrospective answer becomes fiction."
        )

    keys = sorted(
        {
            (held, attribute)
            for held in store.entities()
            if covers(entity, held)
            for attribute in store.attributes(held)
        }
    )
    revisions = tuple(
        Revision(
            entity=held,
            attribute=attribute,
            then=store.effective(held, attribute, on, as_known_at=as_known_at),
            now=store.effective(held, attribute, on),
        )
        for held, attribute in keys
    )
    return Retrospect(
        entity=entity,
        on=on,
        as_known_at=as_known_at,
        revisions=revisions,
        # Deliberately not truncated at as_known_at. The late arrival is
        # precisely the value that reached us *after* the moment in question,
        # so filtering to that moment would hide the only thing worth seeing.
        blindness=blind_spots(store, entity),
    )
