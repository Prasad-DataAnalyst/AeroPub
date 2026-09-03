"""The change bulletin — plan section 31's milestone.

The worked example is the design document's, as in ``test_facts.py``: RWY 34L
landing distance published as 3900 m and reduced by amendment. The no-mock-data
rule governs source data entering the product, not the construction of objects
to test resolution and ranking logic; nothing here stands in for a real
publication.

The assertions that matter most are the ones about what the bulletin refuses to
claim. "Everything that changed" is false for any section not read on both
dates, and a bulletin that omitted them silently would read as a clean bill of
health.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.aip import (
    AipCoverage,
    HoldingState,
    SectionHolding,
    aerodrome_sections,
    section,
)
from aeropub.airac import AiracCycle
from aeropub.bulletin import Attention, between_cycles, compile_bulletin
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.impact import Direction
from aeropub.provenance import SourceRef

N1 = AiracCycle.from_identifier("2609")
N2 = AiracCycle.from_identifier("2610")
DAY_BEFORE = N2.effective_date - timedelta(days=1)


def ref(document: str = "AIP AMDT 09/26", locator: str = "AD 2.13") -> SourceRef:
    return SourceRef(
        source_id="QA-CAA",
        document=document,
        locator=locator,
        retrieved_at=datetime(2026, 10, 11, 14, 23, tzinfo=timezone.utc),
        content_hash="b" * 64,
        parser_id="eaip-eurocontrol",
        parser_version="0.1.0",
    )


def fact(entity, attribute, value, valid_from, valid_to=None,
         precedence=Precedence.AIP, document="AIP AMDT 09/26", locator="AD 2.13"):
    return Fact(entity=entity, attribute=attribute, value=value,
                valid_from=valid_from, valid_to=valid_to,
                source=ref(document, locator), precedence=precedence)


@pytest.fixture
def store():
    facts = FactStore()
    # A degradation, an improvement, an advisory, and one nothing covers.
    facts.add(fact("OTHH/RWY34L", "lda_m", 3900, date(2026, 1, 1), DAY_BEFORE))
    facts.add(fact("OTHH/RWY34L", "lda_m", 3500, N2.effective_date,
                   document="AIP AMDT 10/26"))
    facts.add(fact("OTHH/RWY34L", "toda_m", 4100, date(2026, 1, 1), DAY_BEFORE))
    facts.add(fact("OTHH/RWY34L", "toda_m", 4400, N2.effective_date,
                   document="AIP AMDT 10/26"))
    facts.add(fact("OTHH", "rffs_category", 10, date(2026, 1, 1), DAY_BEFORE,
                   locator="AD 2.6"))
    facts.add(fact("OTHH", "rffs_category", 9, N2.effective_date,
                   document="AIP AMDT 10/26", locator="AD 2.6"))
    facts.add(fact("OTHH/RWY34L", "runway_condition_scheme", "GRF",
                   N1.effective_date, DAY_BEFORE, locator="AD 2.7"))
    return facts


def _coverage(*, omit: tuple[str, ...] = ()) -> AipCoverage:
    return AipCoverage([
        SectionHolding(section=s, entity="OTHH", state=HoldingState.HELD,
                       source=ref("AIP", s.code))
        for s in aerodrome_sections()
        if s.code not in omit
    ])


@pytest.fixture
def complete(store):
    full = _coverage()
    return between_cycles(store, "OTHH", N1, N2,
                          coverage_before=full, coverage_after=full)


class TestTheClaim:
    def test_a_bulletin_with_full_coverage_can_say_it_is_complete(self, complete):
        assert complete.is_conclusive
        assert "Complete for AD 2" in complete.coverage_statement()
        assert complete.blind == ()

    def test_an_unread_section_makes_the_bulletin_inconclusive(self, store):
        partial = _coverage(omit=("AD 2.10",))
        bulletin = between_cycles(store, "OTHH", N1, N2,
                                  coverage_before=partial, coverage_after=partial)
        assert not bulletin.is_conclusive
        assert [s.code for s in bulletin.blind] == ["AD 2.10"]
        assert "NOT complete" in bulletin.coverage_statement()

    def test_a_section_read_on_only_one_date_is_not_comparable(self, store):
        # A change between a section we read and one we did not is not a change
        # we detected; it is a comparison we could not make.
        bulletin = between_cycles(
            store, "OTHH", N1, N2,
            coverage_before=_coverage(), coverage_after=_coverage(omit=("AD 2.13",)),
        )
        assert "AD 2.13" in [s.code for s in bulletin.blind]

    def test_a_section_the_state_does_not_publish_is_still_comparable(self, store):
        # Absent on both dates means the State published nothing either time,
        # which is a comparison with a definite answer.
        absent = AipCoverage([
            SectionHolding(section=s, entity="OTHH", state=HoldingState.HELD,
                           source=ref("AIP", s.code))
            for s in aerodrome_sections() if s.code != "AD 2.16"
        ])
        absent.record(SectionHolding(section=section("AD 2.16"), entity="OTHH",
                                     state=HoldingState.ABSENT,
                                     detail="no helicopter landing area"))
        bulletin = between_cycles(store, "OTHH", N1, N2,
                                  coverage_before=absent, coverage_after=absent)
        assert bulletin.is_conclusive

    def test_without_coverage_completeness_is_not_claimed_either_way(self, store):
        bulletin = between_cycles(store, "OTHH", N1, N2)
        assert not bulletin.is_conclusive
        assert not bulletin.coverage_known
        assert "cannot state whether it is complete" in bulletin.coverage_statement()

    def test_no_changes_reads_differently_when_something_was_not_compared(self):
        empty = FactStore()
        partial = _coverage(omit=("AD 2.10",))
        inconclusive = between_cycles(empty, "OTHH", N1, N2,
                                      coverage_before=partial, coverage_after=partial)
        conclusive = between_cycles(empty, "OTHH", N1, N2,
                                    coverage_before=_coverage(), coverage_after=_coverage())
        assert "No change detected in what was compared" in inconclusive.render()
        assert "No change. Every compared section is as it was" in conclusive.render()


class TestRanking:
    def test_a_degradation_is_ranked_for_action(self, complete):
        attributes = {c.attribute for c in complete.action}
        assert attributes == {"lda_m", "rffs_category"}
        assert all(c.impact.direction is Direction.WORSE for c in complete.action)

    def test_an_improvement_is_reported_as_an_opportunity(self, complete):
        assert [c.attribute for c in complete.opportunities] == ["toda_m"]
        assert complete.opportunities[0].impact.is_opportunity

    def test_an_unmodelled_attribute_asks_for_a_human(self, complete):
        assert [c.attribute for c in complete.needs_human] == ["runway_condition_scheme"]
        assert not complete.needs_human[0].impact.assessed

    def test_unassessed_outranks_improvements_because_it_may_be_either(self, complete):
        assert Attention.ACTION < Attention.REVIEW < Attention.OPPORTUNITY
        order = [c.attention for c in complete.changes]
        assert order == sorted(order)

    def test_within_a_band_changes_read_in_publication_order(self, complete):
        # AD 2.6 before AD 2.13, not alphabetically by attribute.
        assert [c.section.code for c in complete.action] == ["AD 2.6", "AD 2.13"]

    def test_nothing_a_reader_sees_addresses_a_specific_operator(self, complete):
        # Layers one and two carry no operator context. An RFFS downgrade from
        # 9 to 7 is critical at a sole suitable diversion for a 777 and
        # irrelevant to an A320 operator needing Category 6 — the bulletin says
        # what changed and what it means generally, and stops there.
        from aeropub.bulletin import _BAND_HEADINGS

        visible = " ".join(
            [complete.render(), complete.coverage_statement()]
            + [h for h in _BAND_HEADINGS.values()]
            + [a.label for a in Attention]
        ).lower()
        for leaked in ("fleet", "your aircraft", "your network", "tenant", "customer"):
            assert leaked not in visible

    def test_the_band_names_describe_reading_order_not_severity(self):
        # "action" and "opportunity" say what to do about a change. "critical"
        # and "minor" would be claims about an operator nobody has named.
        from aeropub.bulletin import _BAND_HEADINGS

        names = " ".join(_BAND_HEADINGS.values()).lower()
        for severity_word in ("critical", "major", "minor", "severe", "negligible"):
            assert severity_word not in names
        assert set(_BAND_HEADINGS) == set(Attention)


class TestPlacement:
    def test_each_change_is_placed_in_the_section_that_published_it(self, complete):
        placed = {c.attribute: c.section.code for c in complete.changes if c.section}
        assert placed["lda_m"] == "AD 2.13"
        assert placed["toda_m"] == "AD 2.13"
        assert placed["rffs_category"] == "AD 2.6"

    def test_an_unmapped_attribute_is_unplaced_not_guessed(self, complete):
        unplaced = [c for c in complete.changes if c.section is None]
        assert [c.attribute for c in unplaced] == ["runway_condition_scheme"]
        assert "unplaced" in complete.render()

    def test_by_section_groups_in_publication_order_with_unplaced_last(self, complete):
        grouped = complete.by_section()
        codes = [s.code if s else None for s, _ in grouped]
        assert codes == ["AD 2.6", "AD 2.13", None]

    def test_an_unassessed_change_still_reaches_its_sections_readers(self, store):
        # "No rule covers this" must not also mean "nobody hears about it".
        facts = FactStore()
        facts.add(fact("OTHH", "elevation_ft", 35, date(2026, 1, 1), DAY_BEFORE,
                       locator="AD 2.2"))
        facts.add(fact("OTHH", "elevation_ft", 36, N2.effective_date,
                       document="AIP AMDT 10/26", locator="AD 2.2"))
        bulletin = between_cycles(facts, "OTHH", N1, N2)
        reported = bulletin.changes[0]
        assert not reported.impact.assessed
        assert reported.section.code == "AD 2.2"
        assert reported.domains == section("AD 2.2").domains


class TestLenses:
    def test_a_domain_lens_returns_only_what_that_reader_needs(self, complete):
        dispatch = {c.attribute for c in complete.for_domain("dispatch")}
        assert dispatch == {"lda_m", "toda_m", "rffs_category"}
        assert "suitability" in {d for c in complete.changes for d in c.domains}

    def test_a_lens_nobody_is_affected_by_returns_nothing(self, complete):
        assert complete.for_domain("winter") == ()


class TestScope:
    def test_facts_on_a_runway_belong_to_the_aerodromes_bulletin(self, complete):
        assert any(c.entity == "OTHH/RWY34L" for c in complete.changes)
        assert any(c.entity == "OTHH" for c in complete.changes)

    def test_another_aerodrome_is_not_included(self, store):
        store.add(fact("OTBD/RWY15", "lda_m", 4570, date(2026, 1, 1), DAY_BEFORE))
        store.add(fact("OTBD/RWY15", "lda_m", 4000, N2.effective_date))
        bulletin = between_cycles(store, "OTHH", N1, N2)
        assert all(not c.entity.startswith("OTBD") for c in bulletin.changes)

    def test_a_prefix_match_is_not_a_path_match(self, store):
        store.add(fact("OTHHX", "rffs_category", 9, date(2026, 1, 1), DAY_BEFORE))
        store.add(fact("OTHHX", "rffs_category", 7, N2.effective_date))
        bulletin = between_cycles(store, "OTHH", N1, N2)
        assert all(c.entity != "OTHHX" for c in bulletin.changes)

    def test_attributes_can_be_narrowed(self, store):
        bulletin = between_cycles(store, "OTHH", N1, N2, attributes=["rffs_category"])
        assert [c.attribute for c in bulletin.changes] == ["rffs_category"]

    def test_a_supplement_expiring_registers_even_though_nothing_was_published(self):
        # The change a document diff cannot see, and the reason both sides are
        # resolved through the CES rather than compared as documents.
        facts = FactStore()
        facts.add(fact("OTHH/RWY34L", "lda_m", 3900, date(2026, 1, 1)))
        facts.add(fact("OTHH/RWY34L", "lda_m", 3100, date(2026, 1, 1), DAY_BEFORE,
                       precedence=Precedence.SUP, document="AIP SUP 04/26"))
        bulletin = between_cycles(facts, "OTHH", N1, N2)
        assert [c.attribute for c in bulletin.changes] == ["lda_m"]
        assert bulletin.changes[0].change.from_value == 3100
        assert bulletin.changes[0].change.to_value == 3900


class TestReport:
    def test_the_report_names_both_cycles_and_both_dates(self, complete):
        rendered = complete.render()
        assert "AIRAC 2609 → 2610" in rendered
        assert N1.effective_date.isoformat() in rendered
        assert N2.effective_date.isoformat() in rendered

    def test_every_change_carries_both_citations(self, complete):
        rendered = complete.render()
        assert "was: AIP AMDT 09/26" in rendered
        assert "now: AIP AMDT 10/26" in rendered

    def test_an_added_value_has_only_an_after(self, store):
        store.add(fact("OTHH/RWY34L", "papi_angle", 3.0, N2.effective_date,
                       document="AIP AMDT 10/26", locator="AD 2.14"))
        bulletin = between_cycles(store, "OTHH", N1, N2)
        added = [c for c in bulletin.changes if c.attribute == "papi_angle"][0]
        assert added.citations() == (f"now: {added.change.after.source.describe()}",)

    def test_action_is_printed_before_everything_else(self, complete):
        rendered = complete.render()
        positions = [
            rendered.index(heading)
            for heading in ("ACTION —", "NEEDS A HUMAN —", "OPPORTUNITY —")
        ]
        assert positions == sorted(positions)

    def test_the_unread_sections_are_restated_at_the_end(self, store):
        partial = _coverage(omit=("AD 2.10",))
        rendered = between_cycles(store, "OTHH", N1, N2,
                                  coverage_before=partial,
                                  coverage_after=partial).render()
        assert "NOT COMPARED" in rendered
        assert "AD 2.10   Aerodrome obstacles" in rendered

    def test_counts_read_as_english(self, store):
        partial = _coverage(omit=("AD 2.10",))
        rendered = between_cycles(store, "OTHH", N1, N2,
                                  coverage_before=partial, coverage_after=partial).render()
        assert "1 section was not held" in rendered
        assert "1 opportunity  " in rendered or "1 opportunity\n" in rendered
        assert "1 needs a human" in rendered


class TestArguments:
    def test_a_reversed_period_is_refused(self, store):
        with pytest.raises(ValueError, match="precedes"):
            compile_bulletin(store, "OTHH", N2.effective_date, N1.effective_date)

    def test_an_empty_entity_is_refused(self, store):
        with pytest.raises(ValueError, match="non-empty"):
            compile_bulletin(store, "  ", N1.effective_date, N2.effective_date)

    def test_the_entity_is_normalised(self, store):
        bulletin = between_cycles(store, " othh ", N1, N2)
        assert bulletin.entity == "OTHH"
        assert bulletin.changes
