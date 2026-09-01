"""Tests for the AIRAC cycle calendar.

The published AIRAC effective dates below are the ground truth. Everything the
module computes is checked against them or against the invariants the standard
guarantees — 28-day spacing, contiguous coverage, and 13 or 14 cycles a year.
"""

from datetime import date, timedelta

import pytest

from aeropub.airac import (
    CYCLE_DAYS,
    AiracCycle,
    cycle_for,
    cycles_between,
    cycles_in_year,
    cycles_in_year_count,
    current_cycle,
)

# First AIRAC effective date of each year, from the published schedule.
FIRST_EFFECTIVE_DATE = {
    2020: date(2020, 1, 2),
    2021: date(2021, 1, 28),
    2022: date(2022, 1, 27),
    2023: date(2023, 1, 26),
    2024: date(2024, 1, 25),
    2025: date(2025, 1, 23),
    2026: date(2026, 1, 22),
}


class TestPublishedDates:
    @pytest.mark.parametrize("year,expected", sorted(FIRST_EFFECTIVE_DATE.items()))
    def test_first_cycle_of_year_matches_published_schedule(self, year, expected):
        assert AiracCycle(year=year, ordinal=1).effective_date == expected

    @pytest.mark.parametrize("year,expected", sorted(FIRST_EFFECTIVE_DATE.items()))
    def test_identifier_round_trips(self, year, expected):
        cycle = AiracCycle.from_identifier(f"{year % 100:02d}01")
        assert cycle.effective_date == expected
        assert cycle.identifier == f"{year % 100:02d}01"

    def test_2020_has_fourteen_cycles(self):
        # The anchor falls early enough in January that a fourteenth date fits.
        assert cycles_in_year_count(2020) == 14
        assert AiracCycle(year=2020, ordinal=14).effective_date == date(2020, 12, 31)

    @pytest.mark.parametrize("year", [2021, 2022, 2023, 2024, 2025, 2026])
    def test_other_years_have_thirteen_cycles(self, year):
        assert cycles_in_year_count(year) == 13


class TestInvariants:
    def test_consecutive_cycles_are_28_days_apart(self):
        cycle = AiracCycle(year=2020, ordinal=1)
        for _ in range(200):
            following = cycle.next
            assert (following.effective_date - cycle.effective_date).days == CYCLE_DAYS
            cycle = following

    def test_ordinals_are_contiguous_across_a_year_boundary(self):
        last_of_2025 = AiracCycle(year=2025, ordinal=13)
        assert last_of_2025.next == AiracCycle(year=2026, ordinal=1)
        assert AiracCycle(year=2026, ordinal=1).previous == last_of_2025

    def test_cycles_tile_the_calendar_without_gap_or_overlap(self):
        cycle = AiracCycle(year=2024, ordinal=1)
        day = cycle.effective_date
        for _ in range(3 * CYCLE_DAYS):
            assert cycle_for(day) == cycle
            day += timedelta(days=1)
            if day > cycle.expiry_date:
                cycle = cycle.next

    def test_expiry_is_the_day_before_the_next_effective_date(self):
        cycle = AiracCycle(year=2026, ordinal=5)
        assert cycle.expiry_date + timedelta(days=1) == cycle.next.effective_date

    def test_cycles_in_year_are_ordered_and_complete(self):
        cycles = cycles_in_year(2026)
        assert len(cycles) == 13
        assert [c.ordinal for c in cycles] == list(range(1, 14))
        assert cycles == sorted(cycles)


class TestLookup:
    def test_effective_date_itself_belongs_to_its_cycle(self):
        cycle = AiracCycle(year=2026, ordinal=3)
        assert cycle_for(cycle.effective_date) == cycle

    def test_day_before_effective_date_belongs_to_previous_cycle(self):
        cycle = AiracCycle(year=2026, ordinal=3)
        assert cycle_for(cycle.effective_date - timedelta(days=1)) == cycle.previous

    def test_lookup_works_before_the_anchor(self):
        # Flooring must go the right way for dates preceding the anchor.
        cycle = cycle_for(date(2019, 6, 15))
        assert cycle.is_effective_on(date(2019, 6, 15))
        assert cycle.next.effective_date > date(2019, 6, 15)

    def test_current_cycle_accepts_an_explicit_date(self):
        assert current_cycle(date(2026, 1, 22)) == AiracCycle(year=2026, ordinal=1)

    def test_shifted_by_moves_both_directions(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        assert cycle.shifted_by(0) == cycle
        assert cycle.shifted_by(3) == AiracCycle(year=2026, ordinal=4)
        assert cycle.shifted_by(-1) == AiracCycle(year=2025, ordinal=13)


class TestDeadlines:
    def test_distribution_deadline_is_42_days_before_effective(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        assert (cycle.effective_date - cycle.distribution_deadline).days == 42

    def test_major_change_deadline_is_56_days_before_effective(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        assert (cycle.effective_date - cycle.major_change_deadline).days == 56

    def test_recipient_deadline_is_28_days_before_effective(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        assert (cycle.effective_date - cycle.recipient_deadline).days == 28

    def test_major_change_deadline_precedes_distribution_deadline(self):
        cycle = AiracCycle(year=2026, ordinal=7)
        assert cycle.major_change_deadline < cycle.distribution_deadline

    def test_days_until_effective_goes_negative_once_in_force(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        assert cycle.days_until_effective(cycle.effective_date) == 0
        assert cycle.days_until_effective(cycle.distribution_deadline) == 42
        assert cycle.days_until_effective(cycle.effective_date + timedelta(days=5)) == -5


class TestOverdueDetection:
    """The 'should have published by now' half of the watcher's OVERDUE state."""

    def test_not_overdue_before_the_deadline(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        assert not cycle.is_distribution_overdue(cycle.distribution_deadline)

    def test_overdue_the_day_after_the_deadline(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        one_day_late = cycle.distribution_deadline + timedelta(days=1)
        assert cycle.is_distribution_overdue(one_day_late)

    def test_major_changes_become_overdue_earlier(self):
        cycle = AiracCycle(year=2026, ordinal=1)
        between = cycle.major_change_deadline + timedelta(days=1)
        assert cycle.is_distribution_overdue(between, major=True)
        assert not cycle.is_distribution_overdue(between)


class TestValidation:
    @pytest.mark.parametrize("bad", ["261", "26100", "26AB", "", "  "])
    def test_malformed_identifiers_are_rejected(self, bad):
        with pytest.raises(ValueError, match="four digits"):
            AiracCycle.from_identifier(bad)

    def test_identifier_tolerates_surrounding_whitespace(self):
        assert AiracCycle.from_identifier(" 2610 ") == AiracCycle(year=2026, ordinal=10)

    def test_ordinal_zero_is_rejected(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            AiracCycle(year=2026, ordinal=0)

    def test_ordinal_beyond_the_year_is_rejected(self):
        # 2026 has 13 cycles, so 2614 does not exist.
        with pytest.raises(ValueError, match="13 AIRAC cycles"):
            AiracCycle(year=2026, ordinal=14)

    def test_fourteenth_cycle_is_accepted_in_a_fourteen_cycle_year(self):
        assert AiracCycle(year=2020, ordinal=14).identifier == "2014"


class TestRanges:
    def test_cycles_between_covers_the_span(self):
        start = date(2026, 1, 22)
        end = date(2026, 4, 15)
        cycles = list(cycles_between(start, end))
        assert cycles[0] == AiracCycle(year=2026, ordinal=1)
        assert all(c.effective_date <= end for c in cycles)
        assert cycles == sorted(cycles)

    def test_cycles_between_single_day_yields_one_cycle(self):
        day = date(2026, 3, 1)
        assert list(cycles_between(day, day)) == [cycle_for(day)]

    def test_cycles_between_rejects_reversed_range(self):
        with pytest.raises(ValueError, match="must not precede"):
            list(cycles_between(date(2026, 5, 1), date(2026, 1, 1)))
