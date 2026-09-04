"""Chart studies — reconciliation, in both directions.

The finding this module exists for is not "here is what the chart says". It is
the disagreement between two published things: the AIP moved a threshold and
the plate serving it still carries last cycle's revision. So most of what is
tested here is the reconciliation, and the two ways it can go quiet when it
should not:

- a change whose chart consequences nobody has decided, silently producing no
  expectation and therefore no discrepancy;
- a chart registered but never transcribed, whose unread requirements would
  otherwise read as no requirements.

Both would make a partial review print exactly like a complete one, which is
the failure this codebase refuses everywhere else.

The aerodromes, runways and chart names below are fixtures. Every figure cites
a test source, and none of it is a claim about a real procedure.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.changes import Change, ChangeKind
from aeropub.charts import (
    IMPLICATIONS,
    Chart,
    ChartKind,
    ChartRegister,
    ChartReview,
    Minimum,
    Requirement,
    chart_kinds_for,
    compare_minima,
    expectations,
    review_charts,
    serves,
    usable,
)
from aeropub.facts import Fact, Precedence
from aeropub.provenance import SourceRef
from aeropub.suitability import Assessment

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
ON = date(2026, 10, 5)
AD = "XXXX"
RWY = "XXXX/RWY34L"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="chart index",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def fact(entity: str, attribute: str, value) -> Fact:
    return Fact(
        entity=entity,
        attribute=attribute,
        value=value,
        valid_from=date(2026, 1, 1),
        source=ref(locator="AD 2"),
        precedence=Precedence.AIP,
    )


def change(entity: str, attribute: str, before, after) -> Change:
    return Change(
        entity=entity,
        attribute=attribute,
        kind=ChangeKind.MODIFIED,
        before=fact(entity, attribute, before),
        after=fact(entity, attribute, after),
        observed_from=date(2026, 9, 10),
        observed_to=ON,
    )


def chart(identifier: str, kind: ChartKind, **overrides) -> Chart:
    fields = dict(aerodrome=AD, kind=kind, identifier=identifier, source=ref())
    fields.update(overrides)
    return Chart(**fields)


def minimum(category: str, **overrides) -> Minimum:
    fields = dict(category=category, source=ref(locator="minima box"))
    fields.update(overrides)
    return Minimum(**fields)


ILS_34L = chart("ILS OR LOC RWY 34L", ChartKind.IAP, runways=("RWY34L",))
ILS_16R = chart("ILS OR LOC RWY 16R", ChartKind.IAP, runways=("RWY16R",))
DIAGRAM = chart("AERODROME DIAGRAM", ChartKind.AERODROME_DIAGRAM)


# --------------------------------------------------------------------------
# The implication rules
# --------------------------------------------------------------------------


class TestImplications:
    def test_a_specific_rule_beats_a_general_one(self):
        """Longest match wins, or runway_designator is answered by runway_."""
        assert ChartKind.STAR in chart_kinds_for("runway_designator")
        assert ChartKind.STAR not in chart_kinds_for("runway_width")

    def test_a_rule_matches_anywhere_in_the_attribute_name(self):
        """A strict prefix rule would miss displaced_threshold_m entirely."""
        assert ChartKind.IAP in chart_kinds_for("displaced_threshold_m")
        assert ChartKind.IAP in chart_kinds_for("threshold_elevation_ft")

    def test_a_family_of_attributes_is_covered_by_one_prefix(self):
        for attribute in ("declared_tora_m", "declared_asda_m", "declared_lda_m"):
            assert ChartKind.SID in chart_kinds_for(attribute)

    def test_an_undecided_attribute_returns_nothing_rather_than_guessing(self):
        """Empty means the rules have not decided, not that nothing follows.

        The review reports these separately for exactly that reason.
        """
        assert chart_kinds_for("rffs_category") == ()

    def test_a_navaid_change_reaches_the_plate_that_tunes_it(self):
        assert ChartKind.IAP in chart_kinds_for("navaid_frequency_mhz")

    def test_transition_altitude_reaches_every_procedure_chart(self):
        kinds = chart_kinds_for("transition_altitude_ft")
        assert {ChartKind.IAP, ChartKind.SID, ChartKind.STAR} <= set(kinds)

    def test_every_rule_names_at_least_one_kind(self):
        empty = [prefix for prefix, kinds in IMPLICATIONS.items() if not kinds]
        assert empty == [], f"a rule implying nothing is not a rule: {empty}"


class TestServes:
    def test_an_aerodrome_change_reaches_every_chart_there(self):
        assert serves(ILS_34L, AD)
        assert serves(DIAGRAM, AD)

    def test_a_runway_change_reaches_only_the_plates_for_that_runway(self):
        assert serves(ILS_34L, RWY)
        assert not serves(ILS_16R, RWY)

    def test_a_runway_change_still_reaches_the_aerodrome_diagram(self):
        """A threshold move belongs on the diagram whether or not it lists
        runways."""
        assert serves(DIAGRAM, RWY)

    def test_a_chart_at_another_aerodrome_is_never_reached(self):
        elsewhere = Chart(
            aerodrome="YYYY", kind=ChartKind.IAP, identifier="ILS RWY 34L",
            source=ref(),
        )
        assert not serves(elsewhere, RWY)

    def test_a_runway_chart_that_does_not_say_which_runway_is_included(self):
        """The cost of asking is a question; the cost of skipping is a stale
        plate."""
        vague = chart("RNP RWY 34L", ChartKind.IAP)
        assert serves(vague, RWY)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


class TestReconciliation:
    @pytest.fixture
    def register(self) -> ChartRegister:
        return ChartRegister(
            aerodrome=AD, cycle="2610", charts=(ILS_34L, ILS_16R, DIAGRAM)
        )

    def test_a_threshold_move_expects_the_plates_serving_it(self, register):
        found = expectations([change(RWY, "threshold_elevation_ft", 30, 34)], register)
        assert {e.chart.identifier for e in found} == {
            "ILS OR LOC RWY 34L",
            "AERODROME DIAGRAM",
        }

    def test_an_expected_amendment_that_did_not_arrive_is_a_discrepancy(self, register):
        """The finding the module exists for."""
        review = review_charts(
            register, [change(RWY, "threshold_elevation_ft", 30, 34)], on=ON
        )
        assert {d.chart.identifier for d in review.discrepancies} == {
            "ILS OR LOC RWY 34L",
            "AERODROME DIAGRAM",
        }
        assert review.has_findings

    def test_an_amended_chart_satisfies_its_expectation(self):
        amended = chart(
            "ILS OR LOC RWY 34L", ChartKind.IAP, runways=("RWY34L",), amended=True
        )
        register = ChartRegister(aerodrome=AD, charts=(amended,))
        review = review_charts(
            register, [change(RWY, "threshold_elevation_ft", 30, 34)], on=ON
        )
        assert review.discrepancies == ()
        assert review.unexplained == ()

    def test_an_amendment_nothing_explains_is_its_own_finding(self, register):
        """The direction a system built on watching the AIP never produces.

        The likelier reading is not that the State amended a plate for no
        reason — it is that we are missing the AIP change behind it.
        """
        amended = chart(
            "RNP RWY 34L", ChartKind.IAP, runways=("RWY34L",), amended=True
        )
        held = ChartRegister(aerodrome=AD, charts=(amended,))
        review = review_charts(held, [], on=ON)
        assert [u.chart.identifier for u in review.unexplained] == ["RNP RWY 34L"]
        assert "we do not hold it" in review.render()

    def test_a_change_the_rules_have_not_decided_is_reported_not_dropped(
        self, register
    ):
        """Otherwise an incomplete review prints as a clean one."""
        review = review_charts(register, [change(AD, "rffs_category", 9, 7)], on=ON)
        assert [c.attribute for c in review.unmapped] == ["rffs_category"]
        assert not review.is_conclusive
        assert "NOT RECONCILED" in review.render()

    def test_one_change_produces_one_expectation_per_chart(self, register):
        """A chart implicated twice by one change is named once."""
        found = expectations(
            [
                change(RWY, "threshold_elevation_ft", 30, 34),
                change(RWY, "threshold_elevation_ft", 30, 34),
            ],
            register,
        )
        keys = [e.chart.key for e in found]
        assert len(keys) == len(set(keys))

    def test_a_review_with_no_charts_is_a_coverage_gap_not_a_clean_set(self):
        review = review_charts(
            ChartRegister(aerodrome=AD),
            [change(RWY, "threshold_elevation_ft", 30, 34)],
            on=ON,
        )
        assert not review.has_findings
        assert "coverage gap, not a clean chart set" in review.render()

    def test_an_untranscribed_chart_makes_the_review_inconclusive(self, register):
        review = review_charts(register, [], on=ON)
        assert not review.is_conclusive
        assert "unknown is not unchanged" in review.render()

    def test_a_fully_transcribed_review_of_mapped_changes_is_conclusive(self):
        read = chart(
            "ILS OR LOC RWY 34L",
            ChartKind.IAP,
            runways=("RWY34L",),
            amended=True,
            minima=(minimum("C", da_ft=200.0, rvr_m=550.0, line="S-ILS"),),
        )
        register = ChartRegister(aerodrome=AD, charts=(read,))
        review = review_charts(
            register, [change(RWY, "threshold_elevation_ft", 30, 34)], on=ON
        )
        assert review.is_conclusive

    def test_a_discrepancy_shows_the_published_values_behind_it(self, register):
        review = review_charts(
            register, [change(RWY, "threshold_elevation_ft", 30, 34)], on=ON
        )
        text = review.discrepancies[0].describe()
        assert "30" in text and "34" in text


class TestRegister:
    def test_one_register_holds_one_aerodrome(self):
        elsewhere = Chart(
            aerodrome="YYYY", kind=ChartKind.IAP, identifier="ILS RWY 34L",
            source=ref(),
        )
        with pytest.raises(ValueError, match="One register, one aerodrome"):
            ChartRegister(aerodrome=AD, charts=(ILS_34L, elsewhere))

    def test_a_chart_listed_twice_is_refused(self):
        with pytest.raises(ValueError, match="more than once"):
            ChartRegister(aerodrome=AD, charts=(ILS_34L, ILS_34L))

    def test_coverage_counts_transcribed_against_registered(self):
        read = chart(
            "RNP RWY 34L",
            ChartKind.IAP,
            minima=(minimum("C", da_ft=300.0),),
        )
        register = ChartRegister(aerodrome=AD, charts=(ILS_34L, read))
        assert register.coverage() == (1, 2)

    def test_a_chart_needs_the_name_the_state_prints_on_it(self):
        with pytest.raises(ValueError, match="identifier"):
            Chart(aerodrome=AD, kind=ChartKind.IAP, identifier="  ", source=ref())


# --------------------------------------------------------------------------
# Minima
# --------------------------------------------------------------------------


class TestMinima:
    def test_a_category_outside_a_to_e_is_refused(self):
        """The category follows from threshold speed. It is not free text."""
        with pytest.raises(ValueError, match="A-E"):
            minimum("HEAVY")

    def test_a_raised_minimum_is_adverse(self):
        before = chart(
            "ILS RWY 34L",
            ChartKind.IAP,
            minima=(minimum("C", da_ft=200.0, line="S-ILS"),),
        )
        after = chart(
            "ILS RWY 34L",
            ChartKind.IAP,
            minima=(minimum("C", da_ft=250.0, line="S-ILS"),),
        )
        moved = compare_minima(before, after)
        assert len(moved) == 1
        assert moved[0].delta_ft == 50.0
        assert moved[0].is_adverse

    def test_a_lowered_minimum_is_not_adverse(self):
        before = chart(
            "ILS RWY 34L", ChartKind.IAP, minima=(minimum("C", da_ft=250.0),)
        )
        after = chart(
            "ILS RWY 34L", ChartKind.IAP, minima=(minimum("C", da_ft=200.0),)
        )
        assert not compare_minima(before, after)[0].is_adverse

    def test_a_withdrawn_line_is_adverse_and_has_no_delta(self):
        """Losing the LPV line costs the same dispatch a raised DA would.

        Reporting a delta for it would mean subtracting from nothing, which is
        the most dangerous arithmetic available here.
        """
        before = chart(
            "RNP RWY 34L",
            ChartKind.IAP,
            minima=(
                minimum("C", da_ft=250.0, line="LPV"),
                minimum("C", mda_ft=520.0, line="LNAV"),
            ),
        )
        after = chart(
            "RNP RWY 34L",
            ChartKind.IAP,
            minima=(minimum("C", mda_ft=520.0, line="LNAV"),),
        )
        moved = compare_minima(before, after)
        assert len(moved) == 1
        assert moved[0].kind is ChangeKind.REMOVED
        assert moved[0].delta_ft is None
        assert moved[0].is_adverse

    def test_a_raised_rvr_at_the_same_altitude_is_adverse(self):
        before = chart(
            "ILS RWY 34L",
            ChartKind.IAP,
            minima=(minimum("C", da_ft=200.0, rvr_m=550.0),),
        )
        after = chart(
            "ILS RWY 34L",
            ChartKind.IAP,
            minima=(minimum("C", da_ft=200.0, rvr_m=800.0),),
        )
        assert compare_minima(before, after)[0].is_adverse

    def test_lines_are_compared_within_themselves_not_across(self):
        """A precision DA and a circling MDA are different procedures."""
        held = chart(
            "ILS RWY 34L",
            ChartKind.IAP,
            minima=(
                minimum("C", da_ft=200.0, line="S-ILS"),
                minimum("C", mda_ft=700.0, line="CIRCLING"),
            ),
        )
        assert compare_minima(held, held) == ()
        assert held.lowest("C", line="S-ILS").da_ft == 200.0
        assert held.lowest("C", line="CIRCLING").mda_ft == 700.0

    def test_two_different_charts_are_never_compared(self):
        with pytest.raises(ValueError, match="different charts"):
            compare_minima(ILS_34L, ILS_16R)

    def test_an_unchanged_chart_reports_nothing(self):
        held = chart(
            "ILS RWY 34L", ChartKind.IAP, minima=(minimum("C", da_ft=200.0),)
        )
        assert compare_minima(held, held) == ()

    def test_a_minimum_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            Minimum(category="C", source=None, da_ft=200.0)


# --------------------------------------------------------------------------
# Capability
# --------------------------------------------------------------------------


class TestCapability:
    def test_a_held_approval_makes_the_chart_usable(self):
        held = chart(
            "RNP RWY 34L",
            ChartKind.IAP,
            requirements=(Requirement(code="RNP APCH", source=ref()),),
        )
        assert usable(held, ["RNP APCH"]).is_usable

    def test_a_missing_approval_is_not_suitable_and_names_what_is_missing(self):
        held = chart(
            "RNP RWY 34L",
            ChartKind.IAP,
            requirements=(Requirement(code="RNP AR APCH", source=ref()),),
        )
        result = usable(held, ["RNP APCH"])
        assert result.assessment is Assessment.NOT_SUITABLE
        assert result.missing == ("RNP AR APCH",)

    def test_a_near_match_is_not_a_match(self):
        """A fuzzy match here clears an AR approach for an operator without AR."""
        held = chart(
            "RNP RWY 34L",
            ChartKind.IAP,
            requirements=(Requirement(code="RNP AR APCH", source=ref()),),
        )
        assert not usable(held, ["RNP AR"]).is_usable

    def test_an_untranscribed_chart_is_unknown_not_unrestricted(self):
        """An unread plate demanding RNP AR looks exactly like one demanding
        nothing."""
        result = usable(ILS_34L, ["RNP AR APCH"])
        assert result.assessment is Assessment.UNKNOWN
        assert not result.is_usable
        assert "unknown, not unrestricted" in result.describe()

    def test_a_chart_with_no_stated_requirements_but_read_minima_is_usable(self):
        read = chart(
            "ILS RWY 34L", ChartKind.IAP, minima=(minimum("C", da_ft=200.0),)
        )
        assert usable(read, []).is_usable

    def test_a_requirement_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            Requirement(code="RNP AR APCH", source=None)
