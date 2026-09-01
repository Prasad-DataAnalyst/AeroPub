"""Tests for the universal change record.

Facts are constructed here to exercise diff logic; no publication content is
invented. The worked example is the one from the design document, since it
exercises every layer of the CES stack at once.
"""

from datetime import date, datetime, timezone

import pytest

from aeropub.airac import AiracCycle
from aeropub.changes import Change, ChangeKind, diff_cycles, diff_effective
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.provenance import SourceRef

RWY = "OTHH/RWY34L"
LDA = "lda_m"


def ref(document="AIP AD 2.13"):
    return SourceRef(
        source_id="QA-CAA", document=document, locator="AD 2.13",
        retrieved_at=datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
        content_hash="c" * 64, parser_id="ad2-parser", parser_version="1.0",
    )


def fact(value, precedence, valid_from, valid_to=None, doc="AIP AD 2.13",
         entity=RWY, attribute=LDA):
    return Fact(entity=entity, attribute=attribute, value=value, valid_from=valid_from,
                valid_to=valid_to, source=ref(doc), precedence=precedence)


BASE = fact(3900, Precedence.AIP, date(2020, 1, 1))
SUP = fact(3500, Precedence.SUP, date(2026, 9, 1), date(2026, 11, 30), "AIP SUP 14/26")


class TestConstruction:
    def test_a_change_needs_at_least_one_side(self):
        with pytest.raises(ValueError, match="before or an after"):
            Change(RWY, LDA, ChangeKind.MODIFIED, None, None, date(2026, 1, 1), date(2026, 2, 1))

    def test_a_modification_needs_both_sides(self):
        with pytest.raises(ValueError, match="both sides"):
            Change(RWY, LDA, ChangeKind.MODIFIED, BASE, None, date(2026, 1, 1), date(2026, 2, 1))

    def test_an_addition_cannot_have_a_before(self):
        with pytest.raises(ValueError, match="cannot have a before"):
            Change(RWY, LDA, ChangeKind.ADDED, BASE, SUP, date(2026, 1, 1), date(2026, 2, 1))

    def test_a_removal_cannot_have_an_after(self):
        with pytest.raises(ValueError, match="cannot have an after"):
            Change(RWY, LDA, ChangeKind.REMOVED, BASE, SUP, date(2026, 1, 1), date(2026, 2, 1))


class TestDiff:
    def test_a_supplement_taking_effect_is_a_change(self):
        store = FactStore([BASE, SUP])
        changes = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))
        assert len(changes) == 1
        assert changes[0].from_value == 3900
        assert changes[0].to_value == 3500
        assert changes[0].kind is ChangeKind.MODIFIED

    def test_a_supplement_expiring_is_a_change_with_nothing_published(self):
        # Nothing was issued to cause it; a document diff would see nothing at
        # all. This is why the comparison is between effective states.
        store = FactStore([BASE, SUP])
        changes = diff_effective(store, date(2026, 11, 1), date(2026, 12, 15))
        assert len(changes) == 1
        assert (changes[0].from_value, changes[0].to_value) == (3500, 3900)

    def test_no_difference_produces_no_change(self):
        store = FactStore([BASE, SUP])
        assert diff_effective(store, date(2026, 9, 5), date(2026, 9, 20)) == []

    def test_newly_covered_reads_as_added(self):
        store = FactStore([SUP])
        changes = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))
        assert changes[0].kind is ChangeKind.ADDED
        assert changes[0].from_value is None

    def test_no_longer_covered_reads_as_removed(self):
        store = FactStore([SUP])
        changes = diff_effective(store, date(2026, 9, 15), date(2026, 12, 15))
        assert changes[0].kind is ChangeKind.REMOVED
        assert changes[0].to_value is None

    def test_uncovered_on_both_sides_is_not_a_change(self):
        store = FactStore([SUP])
        assert diff_effective(store, date(2020, 1, 1), date(2020, 6, 1)) == []


class TestScoping:
    def test_can_be_limited_to_one_entity(self):
        other = fact(9, Precedence.AIP, date(2020, 1, 1), entity="OTHH",
                     attribute="rffs_category")
        later = fact(7, Precedence.AMDT, date(2026, 9, 1), entity="OTHH",
                     attribute="rffs_category")
        store = FactStore([BASE, SUP, other, later])

        scoped = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15), entity="OTHH")
        assert {c.entity for c in scoped} == {"OTHH"}

    def test_can_be_limited_to_named_attributes(self):
        other = fact(9, Precedence.AIP, date(2020, 1, 1), attribute="rffs_category")
        later = fact(7, Precedence.AMDT, date(2026, 9, 1), attribute="rffs_category")
        store = FactStore([BASE, SUP, other, later])

        scoped = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15),
                                attributes=["rffs_category"])
        assert [c.attribute for c in scoped] == ["rffs_category"]

    def test_results_are_ordered_stably(self):
        store = FactStore([
            BASE, SUP,
            fact(9, Precedence.AIP, date(2020, 1, 1), attribute="rffs_category"),
            fact(7, Precedence.AMDT, date(2026, 9, 1), attribute="rffs_category"),
        ])
        changes = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))
        assert [c.attribute for c in changes] == sorted(c.attribute for c in changes)


class TestCycles:
    def test_cycles_are_sampled_on_their_effective_dates(self):
        before = AiracCycle.from_identifier("2609")
        after = AiracCycle.from_identifier("2610")
        store = FactStore([
            BASE,
            fact(3500, Precedence.AMDT, after.effective_date, doc="AMDT 10/26"),
        ])
        changes = diff_cycles(store, before, after)
        assert len(changes) == 1
        assert changes[0].observed_from == before.effective_date
        assert changes[0].observed_to == after.effective_date


class TestPresentation:
    def test_describe_states_the_movement_without_interpreting_it(self):
        store = FactStore([BASE, SUP])
        change = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))[0]
        assert change.describe() == "lda_m 3900 → 3500"

    def test_describe_handles_addition_and_removal(self):
        store = FactStore([SUP])
        added = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))[0]
        removed = diff_effective(store, date(2026, 9, 15), date(2026, 12, 15))[0]
        assert "published as 3500" in added.describe()
        assert "no longer published" in removed.describe()

    def test_the_citation_on_each_side_is_reachable(self):
        store = FactStore([BASE, SUP])
        change = diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))[0]
        assert change.source_before == "AIP AD 2.13"
        assert change.source_after == "AIP SUP 14/26"


class TestNumericDelta:
    def test_reports_how_far_a_number_moved(self):
        store = FactStore([BASE, SUP])
        assert diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))[0].numeric_delta() == -400

    def test_is_none_for_non_numeric_values(self):
        store = FactStore([
            fact("asphalt", Precedence.AIP, date(2020, 1, 1), attribute="surface"),
            fact("concrete", Precedence.AMDT, date(2026, 9, 1), attribute="surface"),
        ])
        assert diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))[0].numeric_delta() is None

    def test_booleans_are_not_treated_as_numbers(self):
        # True - False is 1 in Python, which would be a meaningless delta.
        store = FactStore([
            fact(True, Precedence.AIP, date(2020, 1, 1), attribute="lvp_available"),
            fact(False, Precedence.AMDT, date(2026, 9, 1), attribute="lvp_available"),
        ])
        assert diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))[0].numeric_delta() is None

    def test_is_none_for_additions_and_removals(self):
        store = FactStore([SUP])
        assert diff_effective(store, date(2026, 8, 1), date(2026, 9, 15))[0].numeric_delta() is None
