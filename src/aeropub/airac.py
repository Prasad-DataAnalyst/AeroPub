"""AIRAC cycle calendar — the time spine every other component consumes.

AIRAC (Aeronautical Information Regulation And Control) fixes aeronautical
information effective dates to a 28-day cycle common to every ICAO State.
Because the dates are predictable years ahead, it is possible to say when a
State *should* have published for a coming cycle — and therefore to detect the
publication that never arrived, which is the ``OVERDUE`` condition the
publication watcher depends on.

This module is pure arithmetic over a published international standard. It
reads no external source, so unlike extracted aeronautical facts it carries no
``SourceRef``: there is nothing to attribute beyond the standard itself.

Reference points
----------------
Anchor
    2 January 2020 is the effective date of AIRAC cycle 2001.
Spacing
    Exactly 28 days between consecutive effective dates.
Numbering
    Cycles are numbered by the calendar year their effective date falls in, so
    a year holds 13 cycles — or 14 when the dates align such that one more fits
    (2020 is such a year).

Distribution deadlines follow ICAO Annex 15 / PANS-AIM: AIRAC information is
distributed at least 42 days before the effective date, with the objective of
reaching recipients at least 28 days ahead. Changes of major significance use
56 days.

The anchor date should be checked against the official ICAO/EUROCONTROL
published schedule before production use. Every other date here derives from
it, so if the anchor is right the rest follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

__all__ = [
    "AiracCycle",
    "CYCLE_DAYS",
    "DISTRIBUTION_LEAD_DAYS",
    "MAJOR_CHANGE_LEAD_DAYS",
    "RECIPIENT_LEAD_DAYS",
    "cycle_for",
    "cycles_apart",
    "cycles_between",
    "cycles_in_year",
    "cycles_between",
    "current_cycle",
]

#: Days between consecutive AIRAC effective dates.
CYCLE_DAYS = 28

#: Effective date of AIRAC cycle 2001, from which all other dates derive.
ANCHOR_DATE = date(2020, 1, 2)
ANCHOR_YEAR = 2020

#: Minimum days before the effective date that AIRAC information is distributed.
DISTRIBUTION_LEAD_DAYS = 42

#: Distribution lead time for changes of major significance.
MAJOR_CHANGE_LEAD_DAYS = 56

#: Objective for information reaching recipients before the effective date.
RECIPIENT_LEAD_DAYS = 28

#: Identifiers use a two-digit year; aeronautical publication is 21st century.
_CENTURY = 2000


def _cycles_since_anchor(day: date) -> int:
    """Whole 28-day cycles between the anchor and ``day``, flooring toward -inf.

    Python's ``//`` already floors for negative operands, which is what dates
    before the anchor need.
    """
    return (day - ANCHOR_DATE).days // CYCLE_DAYS


def _effective_date_at(index: int) -> date:
    """The effective date ``index`` cycles after the anchor."""
    return ANCHOR_DATE + timedelta(days=index * CYCLE_DAYS)


def _first_index_of_year(year: int) -> int:
    """Index of the first AIRAC effective date falling in ``year``."""
    jan_first = date(year, 1, 1)
    index = _cycles_since_anchor(jan_first)
    # _cycles_since_anchor floors, so the date at `index` may precede 1 January.
    if _effective_date_at(index) < jan_first:
        index += 1
    return index


@dataclass(frozen=True, order=True)
class AiracCycle:
    """One AIRAC cycle, identified by year and ordinal within that year."""

    year: int
    ordinal: int

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError(f"AIRAC ordinal must be >= 1, got {self.ordinal}")
        count = cycles_in_year_count(self.year)
        if self.ordinal > count:
            raise ValueError(
                f"{self.year} has {count} AIRAC cycles, "
                f"so ordinal {self.ordinal} does not exist"
            )

    # -- construction ----------------------------------------------------

    @classmethod
    def from_identifier(cls, identifier: str) -> "AiracCycle":
        """Parse a four-character identifier such as ``"2610"``."""
        text = identifier.strip()
        if len(text) != 4 or not text.isdigit():
            raise ValueError(
                f"AIRAC identifier must be four digits (YYNN), got {identifier!r}"
            )
        return cls(year=_CENTURY + int(text[:2]), ordinal=int(text[2:]))

    @classmethod
    def containing(cls, day: date) -> "AiracCycle":
        """The cycle in force on ``day``."""
        effective = _effective_date_at(_cycles_since_anchor(day))
        year = effective.year
        first = _effective_date_at(_first_index_of_year(year))
        ordinal = (effective - first).days // CYCLE_DAYS + 1
        return cls(year=year, ordinal=ordinal)

    # -- identity --------------------------------------------------------

    @property
    def identifier(self) -> str:
        """The conventional four-digit identifier, e.g. ``"2610"``."""
        return f"{self.year % 100:02d}{self.ordinal:02d}"

    def __str__(self) -> str:
        return self.identifier

    # -- dates -----------------------------------------------------------

    @property
    def effective_date(self) -> date:
        """The date this cycle's information takes effect."""
        return _effective_date_at(_first_index_of_year(self.year) + self.ordinal - 1)

    @property
    def expiry_date(self) -> date:
        """The last date this cycle is in force, before the next takes effect."""
        return self.effective_date + timedelta(days=CYCLE_DAYS - 1)

    @property
    def distribution_deadline(self) -> date:
        """Latest date a State should distribute AIRAC information (T-42)."""
        return self.effective_date - timedelta(days=DISTRIBUTION_LEAD_DAYS)

    @property
    def major_change_deadline(self) -> date:
        """Latest distribution date for changes of major significance (T-56)."""
        return self.effective_date - timedelta(days=MAJOR_CHANGE_LEAD_DAYS)

    @property
    def recipient_deadline(self) -> date:
        """Date by which information should have reached recipients (T-28)."""
        return self.effective_date - timedelta(days=RECIPIENT_LEAD_DAYS)

    # -- navigation ------------------------------------------------------

    @property
    def next(self) -> "AiracCycle":
        return AiracCycle.containing(self.effective_date + timedelta(days=CYCLE_DAYS))

    @property
    def previous(self) -> "AiracCycle":
        return AiracCycle.containing(self.effective_date - timedelta(days=1))

    def shifted_by(self, cycles: int) -> "AiracCycle":
        """The cycle ``cycles`` positions away; negative moves backwards."""
        return AiracCycle.containing(
            self.effective_date + timedelta(days=cycles * CYCLE_DAYS)
        )

    # -- watcher support -------------------------------------------------

    def is_effective_on(self, day: date) -> bool:
        return self.effective_date <= day <= self.expiry_date

    def days_until_effective(self, as_of: date) -> int:
        """Days from ``as_of`` to the effective date; negative once in force."""
        return (self.effective_date - as_of).days

    def is_distribution_overdue(self, as_of: date, *, major: bool = False) -> bool:
        """Whether the distribution deadline has passed as of ``as_of``.

        The calendar only knows the deadline. Whether a State actually published
        is the watcher's business — this answers the "should have by now" half of
        the ``OVERDUE`` condition.
        """
        deadline = self.major_change_deadline if major else self.distribution_deadline
        return as_of > deadline


def cycles_in_year_count(year: int) -> int:
    """How many AIRAC cycles have an effective date in ``year`` (13 or 14)."""
    first = _first_index_of_year(year)
    next_first = _first_index_of_year(year + 1)
    return next_first - first


def cycles_in_year(year: int) -> list[AiracCycle]:
    """Every cycle whose effective date falls in ``year``, in order."""
    return [AiracCycle(year=year, ordinal=n) for n in range(1, cycles_in_year_count(year) + 1)]


def cycle_for(day: date) -> AiracCycle:
    """The AIRAC cycle in force on ``day``."""
    return AiracCycle.containing(day)


def current_cycle(today: date | None = None) -> AiracCycle:
    """The cycle in force today, or on ``today`` when supplied."""
    return AiracCycle.containing(today if today is not None else date.today())


def cycles_between(start: date, end: date) -> Iterator[AiracCycle]:
    """Every cycle in force at any point between ``start`` and ``end`` inclusive."""
    if end < start:
        raise ValueError("end must not precede start")
    cycle = AiracCycle.containing(start)
    while cycle.effective_date <= end:
        yield cycle
        cycle = cycle.next


def cycles_apart(earlier: "AiracCycle", later: "AiracCycle") -> int:
    """How many AIRAC cycles separate two, negative where ``later`` precedes.

    Distinct from :func:`cycles_between`, which enumerates the cycles covering
    a span of dates. This counts the gap between two cycles and returns a
    number.

    Cycle identifiers restart each year and a year holds thirteen cycles or
    fourteen, so subtracting ordinals is wrong across a year boundary. The
    effective dates are on a fixed 28-day grid, which is not.
    """
    return (later.effective_date - earlier.effective_date).days // CYCLE_DAYS
