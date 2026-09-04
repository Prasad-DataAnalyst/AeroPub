"""How current the held data is — and why "clear" is not an answer without it.

A confident all-clear computed from an AIP page read fourteen cycles ago is the
failure this whole project exists against, and until this module existed the
platform could produce one. The suitability layer refuses to assess what it does
not hold; nothing refused to assess what it held *from a year ago*, and the
output looked identical.

Staleness is measured in AIRAC cycles, not days
-----------------------------------------------
Thirty days is not a meaningful age for aeronautical data; **one missed cycle
is**. A State publishes amendments on the 28-day grid, so what matters is how
many effective dates have passed since the page was read — each one an
opportunity for an amendment to have landed that nobody went back for.

Three states, and the fourth that is not one
--------------------------------------------
``CURRENT``
    Read within the cycle now in force. No amendment can have been missed.

``AGEING``
    One cycle behind. One amendment could have landed unread. Usable, and worth
    saying.

``STALE``
    Two or more cycles behind. Enough has passed that a clear verdict computed
    from it is a claim about the past, not the present.

``NEVER_READ`` is deliberately in the same enum rather than modelled as an
absence, because the whole point is that consumers must handle it: an aerodrome
nobody has read and an aerodrome read this morning must never fall into the
same branch of an ``if``.

What this does not do
---------------------
It does not go and re-read anything, and it does not know whether the State
actually published an amendment — only the watcher and the change record know
that. It measures the age of our reading against the calendar the publications
follow. An aerodrome that is two cycles behind may be perfectly unchanged; the
point is that nothing here can say so, and saying so anyway is the failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from aeropub.airac import AiracCycle, cycles_apart
from aeropub.entities import covers

__all__ = [
    "AGEING_AFTER_CYCLES",
    "STALE_AFTER_CYCLES",
    "Currency",
    "DataCurrency",
    "assess_currency",
]

#: One cycle behind: an amendment could have landed since the reading.
AGEING_AFTER_CYCLES = 1

#: Two cycles behind. Chosen rather than measured — the distribution deadlines
#: mean a State's amendment reaches recipients before its effective date, so a
#: reader who is two cycles behind has had two full opportunities to collect one
#: and taken neither. Raise it for a State that publishes rarely; lower it for
#: one that amends every cycle. It is a threshold, not a law, and it is exposed
#: so an operator can set it from their own experience of a State.
STALE_AFTER_CYCLES = 2


class Currency(str, Enum):
    """How far behind the publication calendar our reading is."""

    CURRENT = "current"
    AGEING = "ageing"
    STALE = "stale"
    NEVER_READ = "never_read"

    @property
    def is_usable(self) -> bool:
        """Whether a verdict computed from this reading stands on its own.

        ``AGEING`` is usable and worth saying; ``STALE`` is a claim about the
        past wearing the clothes of a claim about the present.
        """
        return self in (Currency.CURRENT, Currency.AGEING)

    @property
    def is_read(self) -> bool:
        return self is not Currency.NEVER_READ


@dataclass(frozen=True, slots=True)
class DataCurrency:
    """The age of what we hold for one entity, against the AIRAC calendar."""

    entity: str
    as_of: date
    newest: datetime | None = None
    """When the most recent value here was read. ``None`` means nothing was."""

    oldest: datetime | None = None
    facts: int = 0
    cycles_behind: int = 0
    """AIRAC cycles between the reading and the cycle now in force.

    Zero for a reading inside the current cycle. Meaningless where nothing was
    read, and :attr:`state` is ``NEVER_READ`` there rather than zero-behind."""

    @property
    def state(self) -> Currency:
        if self.newest is None:
            return Currency.NEVER_READ
        if self.cycles_behind >= STALE_AFTER_CYCLES:
            return Currency.STALE
        if self.cycles_behind >= AGEING_AFTER_CYCLES:
            return Currency.AGEING
        return Currency.CURRENT

    @property
    def is_usable(self) -> bool:
        return self.state.is_usable

    @property
    def spread_cycles(self) -> int:
        """Cycles between the oldest and newest reading here.

        A wide spread means the aerodrome was read piecemeal — AD 2.12 this
        cycle, AD 2.6 four cycles ago — and a document assembled from it is
        current in parts. That is not the same as current.
        """
        if self.newest is None or self.oldest is None:
            return 0
        return cycles_apart(
            AiracCycle.containing(self.oldest.date()),
            AiracCycle.containing(self.newest.date()),
        )

    def describe(self) -> str:
        if self.newest is None:
            return f"{self.entity}: never read"
        cycles = (
            "read this cycle"
            if self.cycles_behind == 0
            else f"{self.cycles_behind} cycle{'s' if self.cycles_behind != 1 else ''} behind"
        )
        spread = (
            f", assembled across {self.spread_cycles} cycles"
            if self.spread_cycles >= AGEING_AFTER_CYCLES
            else ""
        )
        return (
            f"{self.entity}: {self.state.value}, {cycles} "
            f"(newest {self.newest:%Y-%m-%d}){spread}"
        )


def assess_currency(store, entity: str, *, as_of: date | None = None) -> DataCurrency:
    """How current everything held for one entity is.

    Reads the ``retrieved_at`` on each value's citation rather than any
    separate bookkeeping, so it cannot drift from what the citations say. A
    value has a reading date because it has a ``SourceRef``; there is no path
    by which a fact exists without one.
    """
    when = as_of or date.today()
    current = AiracCycle.containing(when)

    newest: datetime | None = None
    oldest: datetime | None = None
    count = 0
    for held in store.entities():
        if not covers(entity, held):
            continue
        for attribute in store.attributes(held):
            for fact in store.history(held, attribute):
                read_at = fact.source.retrieved_at
                count += 1
                if newest is None or read_at > newest:
                    newest = read_at
                if oldest is None or read_at < oldest:
                    oldest = read_at

    behind = (
        cycles_apart(AiracCycle.containing(newest.date()), current)
        if newest is not None
        else 0
    )
    return DataCurrency(
        entity=entity,
        as_of=when,
        newest=newest,
        oldest=oldest,
        facts=count,
        # A reading dated after the current cycle began is not "negative
        # cycles behind"; it is current.
        cycles_behind=max(0, behind),
    )
