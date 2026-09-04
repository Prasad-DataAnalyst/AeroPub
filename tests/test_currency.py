"""How old the held data is, measured against the calendar it follows.

Until this existed the platform could produce a confident all-clear from an AIP
page read fourteen cycles ago, and the output was indistinguishable from one
read this morning. That is the failure the whole project is built against, and
it was sitting inside it.

Staleness is counted in AIRAC cycles rather than days because thirty days is
not a meaningful age for aeronautical data and one missed cycle is. The tests
below therefore work in cycle effective dates, not in weeks.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.airac import AiracCycle, cycles_apart
from aeropub.currency import (
    AGEING_AFTER_CYCLES,
    STALE_AFTER_CYCLES,
    Currency,
    DataCurrency,
    assess_currency,
)
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.provenance import SourceRef

#: 2026-10-05 falls inside AIRAC 2610, effective 2026-10-01.
TODAY = date(2026, 10, 5)
THIS_CYCLE = datetime(2026, 10, 2, tzinfo=timezone.utc)


def read_at(cycles_ago: int) -> datetime:
    """A reading dated inside the cycle ``cycles_ago`` before the current one."""
    cycle = AiracCycle.containing(TODAY).shifted_by(-cycles_ago)
    return datetime(
        cycle.effective_date.year, cycle.effective_date.month,
        cycle.effective_date.day, 12, 0, tzinfo=timezone.utc,
    )


def fact(entity: str, attribute: str, when: datetime) -> Fact:
    return Fact(
        entity=entity, attribute=attribute, value=9,
        valid_from=date(2026, 1, 1), precedence=Precedence.AIP,
        source=SourceRef(
            source_id="TEST", document="AIP AD 2", locator=attribute,
            retrieved_at=when, content_hash="a" * 64,
            parser_id="aip-manifest", parser_version="1",
        ),
    )


class TestCyclesBetween:
    def test_a_cycle_is_zero_from_itself(self):
        cycle = AiracCycle.from_identifier("2610")
        assert cycles_apart(cycle, cycle) == 0

    def test_consecutive_cycles_are_one_apart(self):
        cycle = AiracCycle.from_identifier("2610")
        assert cycles_apart(cycle, cycle.next) == 1
        assert cycles_apart(cycle.next, cycle) == -1

    def test_it_counts_correctly_across_a_year_boundary(self):
        # Identifiers restart each year and a year holds thirteen cycles or
        # fourteen, so subtracting ordinals is wrong here and the effective
        # dates are not.
        late = AiracCycle.containing(date(2026, 12, 20))
        early = AiracCycle.containing(date(2027, 2, 20))
        assert cycles_apart(late, early) == (
            (early.effective_date - late.effective_date).days // 28
        )
        assert cycles_apart(late, early) > 0


class TestCurrencyStates:
    def store(self, cycles_ago: int) -> FactStore:
        return FactStore([fact("AAAA", "rffs_category", read_at(cycles_ago))])

    def test_read_this_cycle_is_current(self):
        held = assess_currency(self.store(0), "AAAA", as_of=TODAY)
        assert held.state is Currency.CURRENT
        assert held.cycles_behind == 0
        assert held.is_usable

    def test_one_cycle_behind_is_ageing_and_still_usable(self):
        held = assess_currency(self.store(AGEING_AFTER_CYCLES), "AAAA", as_of=TODAY)
        assert held.state is Currency.AGEING
        assert held.is_usable

    def test_two_cycles_behind_is_stale_and_not_usable(self):
        held = assess_currency(self.store(STALE_AFTER_CYCLES), "AAAA", as_of=TODAY)
        assert held.state is Currency.STALE
        assert not held.is_usable

    def test_far_behind_reports_how_far(self):
        held = assess_currency(self.store(6), "AAAA", as_of=TODAY)
        assert held.cycles_behind == 6
        assert "6 cycles behind" in held.describe()

    def test_never_read_is_a_state_not_an_absence(self):
        # An aerodrome nobody has read and one read this morning must never
        # fall into the same branch of an if.
        held = assess_currency(FactStore(), "AAAA", as_of=TODAY)
        assert held.state is Currency.NEVER_READ
        assert not held.is_usable
        assert not held.state.is_read
        assert held.newest is None
        assert "never read" in held.describe()

    def test_a_future_dated_reading_is_current_not_negative(self):
        held = assess_currency(
            FactStore([fact("AAAA", "rffs_category",
                            datetime(2026, 10, 28, tzinfo=timezone.utc))]),
            "AAAA", as_of=TODAY,
        )
        assert held.cycles_behind == 0
        assert held.state is Currency.CURRENT


class TestWhatItMeasures:
    def test_it_reads_the_citations_rather_than_separate_bookkeeping(self):
        # A value has a reading date because it has a SourceRef. There is no
        # path by which a fact exists without one, so this cannot drift.
        held = assess_currency(
            FactStore([fact("AAAA", "rffs_category", THIS_CYCLE)]), "AAAA",
            as_of=TODAY,
        )
        assert held.newest == THIS_CYCLE
        assert held.facts == 1

    def test_the_newest_reading_governs(self):
        held = assess_currency(
            FactStore([
                fact("AAAA", "rffs_category", read_at(6)),
                fact("AAAA", "runway_width_m", read_at(0)),
            ]),
            "AAAA", as_of=TODAY,
        )
        assert held.state is Currency.CURRENT

    def test_but_a_piecemeal_reading_is_reported_as_one(self):
        # The trap this catches: an aerodrome looks current because one section
        # was read yesterday, while another is six cycles old. Current in parts
        # is not current.
        held = assess_currency(
            FactStore([
                fact("AAAA", "rffs_category", read_at(6)),
                fact("AAAA", "runway_width_m", read_at(0)),
            ]),
            "AAAA", as_of=TODAY,
        )
        assert held.spread_cycles == 6
        assert "assembled across 6 cycles" in held.describe()

    def test_a_single_reading_has_no_spread(self):
        held = assess_currency(
            FactStore([fact("AAAA", "rffs_category", THIS_CYCLE)]), "AAAA",
            as_of=TODAY,
        )
        assert held.spread_cycles == 0
        assert "assembled across" not in held.describe()

    def test_it_reaches_everything_on_the_aerodrome(self):
        held = assess_currency(
            FactStore([
                fact("AAAA", "rffs_category", THIS_CYCLE),
                fact("AAAA/RWY34L", "pcn", THIS_CYCLE),
            ]),
            "AAAA", as_of=TODAY,
        )
        assert held.facts == 2

    def test_containment_runs_one_way(self):
        # Asking about a runway does not pull in the aerodrome's own values.
        held = assess_currency(
            FactStore([
                fact("AAAA", "rffs_category", THIS_CYCLE),
                fact("AAAA/RWY34L", "pcn", THIS_CYCLE),
            ]),
            "AAAA/RWY34L", as_of=TODAY,
        )
        assert held.facts == 1

    def test_another_aerodrome_is_not_counted(self):
        held = assess_currency(
            FactStore([fact("BBBB", "rffs_category", THIS_CYCLE)]), "AAAA",
            as_of=TODAY,
        )
        assert held.state is Currency.NEVER_READ


class TestThresholdsAreDeclaredNotHidden:
    def test_they_are_exposed_so_an_operator_can_set_them(self):
        # A threshold, not a law: raise it for a State that publishes rarely.
        assert AGEING_AFTER_CYCLES < STALE_AFTER_CYCLES

    def test_the_state_boundaries_follow_the_thresholds(self):
        for behind, expected in (
            (0, Currency.CURRENT),
            (AGEING_AFTER_CYCLES, Currency.AGEING),
            (STALE_AFTER_CYCLES, Currency.STALE),
        ):
            held = DataCurrency(
                entity="AAAA", as_of=TODAY, newest=THIS_CYCLE,
                oldest=THIS_CYCLE, facts=1, cycles_behind=behind,
            )
            assert held.state is expected
