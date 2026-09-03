"""What changes next — including the changes nobody will announce.

Every other part of this system looks backwards: a bulletin says what moved
between two cycles, a dossier says what is true now. Both answer questions
somebody thought to ask.

This one answers the question nobody is told. **A supplement expiring publishes
nothing.** A NOTAM lapsing publishes nothing. On the morning the window closes,
the layer beneath resurfaces and the operationally true value changes, with no
message issued, no AIRAC date, and nothing in anyone's inbox. An operator who
planned around a restriction for three months finds out by accident, or does
not find out at all — and the reverse case is worse: a temporarily *increased*
figure quietly reverting to a shorter one.

The Consolidated Effective State already knows this. Asking it for a future
date costs nothing and is exact — no forecasting, no model, no estimate.
Walking the layer boundaries forward turns that into a list.

Three kinds of transition, and the distinction is the point
-----------------------------------------------------------
=================  ========================================================
``PUBLISHED``      A layer begins. Somebody issued something, and it will
                   reach an operator through the normal channels
``REVERSION``      A layer ends and the one beneath resurfaces. **Nothing is
                   published.** This is the class that exists nowhere else
``WITHDRAWAL``     A layer ends with nothing beneath it. The value becomes
                   unknown, and must not be carried forward
=================  ========================================================

What this is not
----------------
Not a prediction. It states what the publications already in hand imply about
future dates, and it is exactly as complete as what we hold: a NOTAM issued
tomorrow changes it. :attr:`Horizon.as_known_at` records the belief it was
computed from, so a horizon can be reproduced later rather than argued about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from aeropub.aip import Section, section_for_attribute
from aeropub.changes import Change, ChangeKind
from aeropub.entities import covers, normalise
from aeropub.facts import Fact, FactStore
from aeropub.impact import Impact, assess

__all__ = ["Horizon", "Transition", "Trigger", "horizon"]

#: How far ahead to look unless asked otherwise. Three AIRAC cycles: far enough
#: to cover the planning horizon a schedule is built on, near enough that what
#: we hold is still most of the story.
DEFAULT_DAYS = 84


class Trigger(str, Enum):
    """Why the effective state changes on a date."""

    PUBLISHED = "published"
    """A layer begins. Somebody issued something."""

    REVERSION = "reversion"
    """A layer ends and the one beneath resurfaces. Nothing is published to
    say so, which is why this class exists separately."""

    WITHDRAWAL = "withdrawal"
    """A layer ends with nothing beneath it. The value becomes unknown, and
    the previous figure must not be carried forward."""

    @property
    def is_announced(self) -> bool:
        """Whether an operator will hear about this through normal channels."""
        return self is Trigger.PUBLISHED


@dataclass(frozen=True, slots=True)
class Transition:
    """One dated change in the effective state, and what causes it."""

    on: date
    entity: str
    attribute: str
    trigger: Trigger
    impact: Impact
    section: Section | None = None
    days_away: int = 0

    @property
    def before(self) -> Fact | None:
        return self.impact.change.before

    @property
    def after(self) -> Fact | None:
        return self.impact.change.after

    @property
    def is_announced(self) -> bool:
        return self.trigger.is_announced

    def describe(self) -> str:
        where = self.section.code if self.section else "unplaced"
        return (
            f"{self.on:%Y-%m-%d} (T+{self.days_away})  {where}  "
            f"{self.impact.summary}"
        )

    def why(self) -> str:
        """What causes it, in terms of the publication layers involved."""
        if self.trigger is Trigger.REVERSION:
            expiring = self.before.precedence.name if self.before else "a layer"
            beneath = self.after.precedence.name if self.after else "nothing"
            return (
                f"The {expiring} layer expires and the {beneath} beneath it "
                "resurfaces. Nothing will be published to announce this."
            )
        if self.trigger is Trigger.WITHDRAWAL:
            expiring = self.before.precedence.name if self.before else "a layer"
            return (
                f"The {expiring} layer expires with nothing beneath it. The value "
                "becomes unknown — do not carry the previous figure forward."
            )
        source = self.after.source.document if self.after else "a publication"
        return f"Published: {source}."


@dataclass(frozen=True, slots=True)
class Horizon:
    """Every dated change ahead, from what is already held."""

    entity: str
    from_date: date
    through: date
    transitions: tuple[Transition, ...]
    as_known_at: datetime | None = None
    """The belief this was computed from. A horizon is only as complete as what
    was held when it was taken, and this is what makes it reproducible."""

    @property
    def unannounced(self) -> tuple[Transition, ...]:
        """The ones nobody will be told about. The reason this module exists."""
        return tuple(t for t in self.transitions if not t.is_announced)

    @property
    def announced(self) -> tuple[Transition, ...]:
        return tuple(t for t in self.transitions if t.is_announced)

    def within(self, days: int) -> tuple[Transition, ...]:
        return tuple(t for t in self.transitions if t.days_away <= days)

    def on(self, day: date) -> tuple[Transition, ...]:
        return tuple(t for t in self.transitions if t.on == day)

    def for_domain(self, domain: str) -> tuple[Transition, ...]:
        return tuple(
            t
            for t in self.transitions
            if domain in (t.impact.domains or (t.section.domains if t.section else ()))
        )

    def summary(self) -> dict[str, int]:
        return {
            "transitions": len(self.transitions),
            "unannounced": len(self.unannounced),
            "announced": len(self.announced),
            "within_7_days": len(self.within(7)),
            "within_28_days": len(self.within(28)),
        }

    def render(self) -> str:
        counts = self.summary()
        lines = [
            f"FORWARD VIEW — {self.entity}",
            f"{self.from_date:%Y-%m-%d} through {self.through:%Y-%m-%d}"
            f"  ({(self.through - self.from_date).days} days)",
            "",
        ]
        if not self.transitions:
            lines.append(
                "No dated change ahead in what is held. That is not a forecast: "
                "a NOTAM issued tomorrow would change it."
            )
            return "\n".join(lines)

        lines.append(
            f"{counts['transitions']} dated changes ahead  ·  "
            f"{counts['unannounced']} of them will not be announced"
        )

        if self.unannounced:
            lines += [
                "",
                "NOTHING WILL BE PUBLISHED TO TELL YOU ABOUT THESE",
            ]
            for item in self.unannounced:
                lines.append(f"  {item.describe()}")
                lines.append(f"      {item.why()}")
                lines.append(f"      {item.impact.consequence}")
                if item.before is not None:
                    lines.append(f"      expiring: {item.before.source.describe()}")

        if self.announced:
            lines += ["", "PUBLISHED — these arrive through the normal channels"]
            for item in self.announced:
                lines.append(f"  {item.describe()}")
                lines.append(f"      {item.why()}")

        lines += [
            "",
            "Computed from what is held now. It is exact about those "
            "publications and silent about any not yet issued.",
        ]
        return "\n".join(lines)


def _trigger(before: Fact | None, after: Fact | None) -> Trigger:
    """Why the state changed, read from the layers on either side."""
    if after is None:
        return Trigger.WITHDRAWAL
    if before is None:
        return Trigger.PUBLISHED
    # A lower layer surfacing means the one above it ended, and an ending
    # window is never published — that is the whole distinction.
    if after.precedence < before.precedence:
        return Trigger.REVERSION
    return Trigger.PUBLISHED


def _boundaries(store: FactStore, entity: str, start: date, end: date) -> list[date]:
    """Dates on which the effective state could change.

    Only the edges of the windows already held. Everything between two edges
    resolves identically, so walking day by day would do the same work and
    report the same answer more slowly.
    """
    dates: set[date] = set()
    for fact in store:
        if not covers(entity, fact.entity):
            continue
        if start < fact.valid_from <= end:
            dates.add(fact.valid_from)
        if fact.valid_to is not None:
            # The state changes the day *after* a window closes.
            reverts = fact.valid_to + timedelta(days=1)
            if start < reverts <= end:
                dates.add(reverts)
    return sorted(dates)


def horizon(
    store: FactStore,
    entity: str,
    *,
    from_date: date | None = None,
    days: int = DEFAULT_DAYS,
    through: date | None = None,
    as_known_at: datetime | None = None,
) -> Horizon:
    """Every dated change ahead for one entity and everything beneath it.

    Exact rather than predictive: it evaluates the CES on each date a held
    window opens or closes, and reports the differences. Nothing is
    extrapolated, and nothing not yet published is guessed at.
    """
    key = normalise(entity)
    if not key:
        raise ValueError("entity must be a non-empty string")
    start = from_date or date.today()
    end = through or (start + timedelta(days=days))
    if end < start:
        raise ValueError(f"through ({end}) precedes from_date ({start})")

    pairs = sorted(
        {(f.entity, f.attribute) for f in store if covers(key, f.entity)}
    )
    transitions: list[Transition] = []

    for day in _boundaries(store, key, start, end):
        previous = day - timedelta(days=1)
        for candidate, attribute in pairs:
            was = store.effective(candidate, attribute, previous, as_known_at=as_known_at)
            now = store.effective(candidate, attribute, day, as_known_at=as_known_at)
            if was is None and now is None:
                continue
            if was is not None and now is not None and was.value == now.value:
                continue

            if was is None:
                kind = ChangeKind.ADDED
            elif now is None:
                kind = ChangeKind.REMOVED
            else:
                kind = ChangeKind.MODIFIED

            change = Change(
                entity=candidate,
                attribute=attribute,
                kind=kind,
                before=was,
                after=now,
                observed_from=previous,
                observed_to=day,
            )
            transitions.append(
                Transition(
                    on=day,
                    entity=candidate,
                    attribute=attribute,
                    trigger=_trigger(was, now),
                    impact=assess(change),
                    section=section_for_attribute(attribute),
                    days_away=(day - start).days,
                )
            )

    transitions.sort(key=lambda t: (t.on, t.entity, t.attribute))
    return Horizon(
        entity=key,
        from_date=start,
        through=end,
        transitions=tuple(transitions),
        as_known_at=as_known_at,
    )
