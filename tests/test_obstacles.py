"""Obstacles — the arithmetic that is exact, and the geometry that is refused.

The plan calls the obstacle alert the highest-value single alert in the
platform. What makes it computable is that the decisive number — the climb
gradient required to clear an obstacle — is exact arithmetic on two published
figures, against criteria that agree between ICAO PANS-OPS and FAA TERPS:
a 2.5% obstacle identification surface, a 0.8% minimum obstacle clearance, and
a 3.3% standard gradient that is exactly the sum of the two.

What is tested just as hard is what the module will not answer. Whether an
obstacle lies inside the protected departure area needs the full PANS-OPS
construction, and a confident approximation of that is a confident answer about
whether an obstacle matters at all.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.obstacles import (
    METRES_PER_NM,
    MOC_PERCENT,
    OIS_PERCENT,
    OLS_INSTRUMENT_DEPARTURE,
    PANS_OPS_STRAIGHT,
    STANDARD_PDG_PERCENT,
    DepartureArea,
    Obstacle,
    Penetration,
    Position,
    compare_cycles,
    decompose,
    penetrates_ois,
    required_gradient,
    review_runway,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
FEET = 3.280839895


def ref(document: str = "AIP AD 2.10") -> SourceRef:
    return SourceRef(
        source_id="TEST", document=document, locator="obstacle table",
        retrieved_at=NOW, content_hash="a" * 64,
        parser_id="test", parser_version="1",
    )


def obstacle(identifier="OBS-1", *, feet=None, nm=None, **overrides) -> Obstacle:
    fields = dict(identifier=identifier, source=ref(), kind="crane")
    if feet is not None:
        fields["height_above_der_m"] = feet / FEET
    if nm is not None:
        fields["distance_from_der_m"] = nm * METRES_PER_NM
    fields.update(overrides)
    return Obstacle(**fields)


def plane(designator: str, gradient: float | None = None) -> AircraftType:
    if gradient is None:
        return AircraftType(designator=designator)
    return AircraftType(designator=designator).with_characteristics([
        Characteristic(attribute="climb_gradient_pct", value=gradient,
                       source=ref("Company performance manual"),
                       origin=Origin.OPERATOR)
    ])


class TestTheCriteriaAreTheConstruction:
    def test_the_standard_gradient_is_the_surface_plus_the_clearance(self):
        # 3.3 = 2.5 + 0.8 is not a coincidence, it is how a promulgated
        # gradient is built. Encoding it as three independent numbers would let
        # them drift apart.
        assert STANDARD_PDG_PERCENT == OIS_PERCENT + MOC_PERCENT
        assert STANDARD_PDG_PERCENT == pytest.approx(3.3)

    def test_the_surface_is_the_published_forty_to_one(self):
        assert OIS_PERCENT == 2.5
        # 2.5% is 152 ft per nautical mile, which is how a chart states it.
        assert round(OIS_PERCENT / 100 * METRES_PER_NM * FEET) == 152


class TestRequiredGradient:
    def test_the_plans_own_worked_example(self):
        # "412 ft AGL at 2.1 NM" — the alert the plan describes as the
        # highest-value one in the platform.
        assert required_gradient(obstacle(feet=412, nm=2.1)) == pytest.approx(4.03, abs=0.01)

    def test_an_obstacle_at_the_surface_needs_exactly_the_standard(self):
        # An obstacle sitting on the 2.5% surface needs 2.5 + 0.8 = the
        # standard gradient. That is the definition working.
        on_surface = obstacle(feet=OIS_PERCENT / 100 * METRES_PER_NM * FEET, nm=1.0)
        assert required_gradient(on_surface) == pytest.approx(STANDARD_PDG_PERCENT, abs=0.01)

    def test_the_clearance_is_added_not_assumed_away(self):
        # The subtle case, and the one that catches people. 412 ft at 2.1 NM is
        # 196 ft/NM — just UNDER the 200 ft/NM standard — and it still requires
        # a steeper gradient, because the standard already contains the
        # clearance.
        crane = obstacle(feet=412, nm=2.1)
        own = (crane.height_above_der_m / crane.distance_from_der_m) * 100
        assert own < STANDARD_PDG_PERCENT
        assert required_gradient(crane) > STANDARD_PDG_PERCENT

    def test_a_taller_obstacle_at_the_same_distance_needs_more(self):
        assert required_gradient(obstacle(feet=600, nm=2.1)) > required_gradient(
            obstacle(feet=412, nm=2.1)
        )

    def test_the_same_obstacle_further_out_needs_less(self):
        assert required_gradient(obstacle(feet=412, nm=4.0)) < required_gradient(
            obstacle(feet=412, nm=2.1)
        )

    def test_a_missing_figure_gives_no_gradient(self):
        # A gradient computed from a guessed distance is worse than none.
        assert required_gradient(obstacle(feet=412)) is None
        assert required_gradient(obstacle(nm=2.1)) is None

    def test_a_zero_distance_gives_no_gradient_rather_than_infinity(self):
        assert required_gradient(obstacle(feet=412, nm=0.0)) is None


class TestPenetration:
    def test_an_obstacle_above_the_surface_penetrates(self):
        assert penetrates_ois(obstacle(feet=412, nm=2.1)) is Penetration.PENETRATES

    def test_an_obstacle_below_it_is_clear(self):
        assert penetrates_ois(obstacle(feet=100, nm=4.0)) is Penetration.CLEAR

    def test_unmeasured_is_not_clear(self):
        # "We did not check" and "it is fine" are opposite answers.
        assert penetrates_ois(obstacle(feet=412)) is Penetration.UNKNOWN

    def test_the_default_origin_is_the_conservative_one(self):
        # FAA TERPS starts the surface at DER elevation, ICAO PANS-OPS 5 m
        # above. The lower surface reports more obstacles as penetrating, which
        # is the right direction for a check whose false negative is an
        # aeroplane climbing into something.
        marginal = obstacle(feet=(OIS_PERCENT / 100 * 2000 + 2) * FEET, nm=2000 / METRES_PER_NM)
        assert penetrates_ois(marginal) is Penetration.PENETRATES
        assert penetrates_ois(marginal, ois_origin_m=5.0) is Penetration.CLEAR


class TestProvenance:
    def test_an_obstacle_without_a_source_cannot_exist(self):
        with pytest.raises(TypeError) as caught:
            Obstacle(identifier="CRANE-1", source=None)
        assert "a rumour" in str(caught.value)

    def test_an_unnamed_obstacle_is_refused(self):
        with pytest.raises(ValueError):
            Obstacle(identifier="  ", source=ref())

    def test_negative_dimensions_are_refused(self):
        with pytest.raises(ValueError):
            obstacle(feet=-10, nm=2.0)

    def test_heights_are_kept_above_the_runway_end_not_above_sea_level(self):
        # Converting an elevation without the runway end elevation in hand is
        # the commonest way to get this wrong, so the module never does it.
        assert "height_above_der_m" in Obstacle.__annotations__
        assert not any("elevation" in f for f in Obstacle.__annotations__)


class TestCycleComparison:
    def test_a_new_obstacle_reads_as_new(self):
        changes = compare_cycles([], [obstacle("CRANE-1", feet=412, nm=2.1)])
        assert changes[0].appeared
        assert "NEW" in changes[0].describe()

    def test_a_removed_obstacle_reads_as_removed(self):
        changes = compare_cycles([obstacle("CRANE-1", feet=412, nm=2.1)], [])
        assert changes[0].removed

    def test_an_obstacle_that_grew_is_the_change_that_costs_gradient(self):
        changes = compare_cycles(
            [obstacle("CRANE-1", feet=200, nm=2.1)],
            [obstacle("CRANE-1", feet=412, nm=2.1)],
        )
        assert changes[0].raised
        assert "RAISED" in changes[0].describe()

    def test_an_extended_crane_is_visible_as_one_thing_extended(self):
        # A crane extended four times is one works programme, not four
        # unrelated messages.
        changes = compare_cycles(
            [obstacle("CRANE-1", feet=412, nm=2.1, valid_to=date(2026, 10, 1))],
            [obstacle("CRANE-1", feet=412, nm=2.1, valid_to=date(2026, 12, 3))],
        )
        assert changes[0].extended
        assert "EXTENDED" in changes[0].describe()

    def test_identity_is_the_states_own_identifier(self):
        # Matching on position would be a guess about whether two readings
        # describe one obstacle, and a wrong guess reads as a removal plus an
        # appearance — exactly the alert somebody would act on.
        changes = compare_cycles(
            [obstacle("CRANE-1", feet=412, nm=2.1)],
            [obstacle("CRANE-1", feet=412, nm=2.3)],
        )
        assert len(changes) == 1
        assert not changes[0].appeared and not changes[0].removed

    def test_an_unchanged_obstacle_is_kept_but_not_flagged(self):
        changes = compare_cycles(
            [obstacle("CRANE-1", feet=412, nm=2.1)],
            [obstacle("CRANE-1", feet=412, nm=2.1)],
        )
        assert not changes[0].changed


class TestFleetExposure:
    def review(self, *fleet):
        return review_runway(
            "RWY16", [obstacle("CRANE-1", feet=412, nm=2.1)], fleet=fleet
        )

    def test_it_names_the_types_that_cannot_make_the_gradient(self):
        exposed = self.review(plane("B77W", 3.1), plane("A359", 3.6)).exposure()
        assert exposed.incapable == ("A359", "B77W")
        assert exposed.capable == ()

    def test_a_type_that_can_make_it_is_not_flagged(self):
        exposed = self.review(plane("A20N", 5.0)).exposure()
        assert exposed.capable == ("A20N",)
        assert exposed.incapable == ()

    def test_a_type_with_no_gradient_held_is_unassessed_not_capable(self):
        # Certified performance stays with the operator under plan decision D.
        # Without it the question is reported unanswered, never as "fine".
        exposed = self.review(plane("GL7T")).exposure()
        assert exposed.unassessed == ("GL7T",)
        assert exposed.capable == ()
        assert not exposed.is_conclusive
        assert "no climb gradient held" in exposed.describe()

    def test_an_operator_gradient_is_marked_as_theirs(self):
        held = plane("B77W", 3.1).get("climb_gradient_pct")
        assert held.origin is Origin.OPERATOR
        assert not held.origin.is_redistributable

    def test_no_fleet_gives_no_exposure_rather_than_a_clean_one(self):
        assert review_runway("RWY16", [obstacle(feet=412, nm=2.1)]).exposure().capable == ()


class TestReview:
    def test_the_governing_obstacle_is_the_steepest(self):
        review = review_runway("RWY16", [
            obstacle("MAST", feet=200, nm=4.0),
            obstacle("CRANE", feet=412, nm=2.1),
        ])
        assert review.governing.identifier == "CRANE"
        assert review.exceeds_standard

    def test_an_obstacle_within_the_standard_does_not_exceed_it(self):
        review = review_runway("RWY16", [obstacle("MAST", feet=100, nm=4.0)])
        assert not review.exceeds_standard

    def test_no_obstacles_held_is_a_coverage_gap_not_a_clear_sector(self):
        printed = review_runway("RWY16", []).render()
        assert "coverage gap" in printed
        assert "not a clear departure sector" in printed

    def test_obstacles_without_positions_are_unmeasured_not_clear(self):
        review = review_runway("RWY16", [obstacle("VAGUE")])
        assert len(review.unmeasured) == 1
        printed = review.render()
        assert "not clear, they are unmeasured" in printed

    def test_with_no_area_given_it_says_every_obstacle_was_counted(self):
        # The conservative reading, and it must not read as a statement that
        # they all lie in the departure area.
        printed = review_runway("RWY16", [obstacle(feet=412, nm=2.1)]).render()
        assert "No protected area was given" in printed
        assert "every measurable obstacle is counted" in printed

    def test_the_eosid_is_still_not_computed_and_says_why(self):
        # This one genuinely is certified engineering, and the plan agrees.
        printed = review_runway("RWY16", [obstacle(feet=412, nm=2.1)]).render()
        assert "engine-out net flight path is not computed" in printed
        assert "certified engineering" in printed

    def test_one_gradient_is_quoted_for_one_obstacle(self):
        # The bare module function does not know the runway bearing and the
        # review does, so calling the wrong one printed two different gradients
        # for the same obstacle in the same document.
        import re

        printed = review_runway(
            "RWY16", [obstacle("CRANE", feet=412, nm=2.1, bearing_from_der_deg=168)],
            runway_bearing_deg=160, area=PANS_OPS_STRAIGHT,
        ).render()
        quoted = set(re.findall(r"(\d+\.\d+)%", printed))
        # 2.5 is the surface, 3.3 the standard; exactly one gradient beyond.
        assert len(quoted - {"2.5", "3.3"}) == 1

    def test_the_render_leads_with_the_governing_gradient(self):
        printed = review_runway("RWY16", [obstacle(feet=412, nm=2.1)]).render()
        assert printed.index("Governing gradient") < printed.index("PENETRATES")


class TestUnits:
    def test_metres_convert_to_the_units_charts_use(self):
        crane = obstacle(feet=412, nm=2.1)
        assert crane.height_ft == pytest.approx(412, abs=0.5)
        assert crane.distance_nm == pytest.approx(2.1, abs=0.01)

    def test_a_temporary_obstacle_knows_it_is_one(self):
        assert obstacle(feet=412, nm=2.1, valid_to=date(2026, 12, 3)).is_temporary
        assert not obstacle(feet=412, nm=2.1).is_temporary


# --------------------------------------------------------------------------
# The protected area — computed, against a named convention
# --------------------------------------------------------------------------


class TestDecomposition:
    def test_an_obstacle_dead_ahead_is_all_along_track(self):
        resolved = decompose(distance_m=3704.0, bearing_deg=160,
                             runway_bearing_deg=160)
        assert resolved.along_track_m == pytest.approx(3704.0)
        assert resolved.offset_m == pytest.approx(0.0, abs=1e-6)

    def test_an_obstacle_abeam_is_all_lateral(self):
        resolved = decompose(distance_m=1000.0, bearing_deg=250,
                             runway_bearing_deg=160)
        assert resolved.along_track_m == pytest.approx(0.0, abs=1e-6)
        assert resolved.offset_m == pytest.approx(1000.0)

    def test_either_side_gives_the_same_offset(self):
        left = decompose(distance_m=1000.0, bearing_deg=130, runway_bearing_deg=160)
        right = decompose(distance_m=1000.0, bearing_deg=190, runway_bearing_deg=160)
        assert left.offset_m == pytest.approx(right.offset_m)
        assert left.lateral_m == pytest.approx(-right.lateral_m)

    def test_an_obstacle_behind_the_der_is_recognised(self):
        behind = decompose(distance_m=1000.0, bearing_deg=340,
                           runway_bearing_deg=160)
        assert behind.is_behind


class TestGradientIsMeasuredAlongTrack:
    def off_to_one_side(self) -> Obstacle:
        return obstacle("CRANE", feet=412, nm=2.1, bearing_from_der_deg=190)

    def test_a_radial_distance_under_reports_the_gradient(self):
        # The bug this fixes. An obstacle 30 degrees off the runway bearing at
        # 2.1 NM radial is only 1.82 NM along track, so it needs a steeper
        # gradient than the radial figure suggests — and under-reporting a
        # required climb gradient errs in the direction that flies an aeroplane
        # into something.
        crane = self.off_to_one_side()
        radial = required_gradient(crane)
        along = required_gradient(crane, runway_bearing_deg=160)
        assert along > radial
        assert along == pytest.approx(4.53, abs=0.01)

    def test_without_bearings_the_held_distance_is_taken_as_along_track(self):
        # Which is how AD 2.10 usually gives it.
        plain = obstacle(feet=412, nm=2.1)
        assert required_gradient(plain, runway_bearing_deg=160) == required_gradient(plain)

    def test_an_obstacle_behind_the_der_gets_no_gradient(self):
        # Arithmetic about a climb that has already happened.
        behind = obstacle(feet=412, nm=2.1, bearing_from_der_deg=340)
        assert required_gradient(behind, runway_bearing_deg=160) is None

    def test_penetration_is_measured_along_track_too(self):
        crane = self.off_to_one_side()
        # Same obstacle, nearer down the path, so it penetrates more surely.
        assert penetrates_ois(crane, runway_bearing_deg=160) is Penetration.PENETRATES


class TestDepartureArea:
    def test_the_area_starts_150_m_either_side_at_the_der(self):
        assert PANS_OPS_STRAIGHT.half_width_at(0.0) == 150.0
        assert OLS_INSTRUMENT_DEPARTURE.half_width_at(0.0) == 150.0

    def test_the_two_conventions_diverge_and_that_is_the_point(self):
        # Both published surfaces use the number 15 and mean different things:
        # PANS-OPS splays 15 per cent each side, the Annex 14 instrument
        # departure surface 15 degrees. At 2.1 NM that is 733 m against 1192 m.
        at = 2.1 * METRES_PER_NM
        assert PANS_OPS_STRAIGHT.half_width_at(at) == pytest.approx(733, abs=2)
        assert OLS_INSTRUMENT_DEPARTURE.half_width_at(at) == pytest.approx(1192, abs=2)

    def test_an_area_must_name_exactly_one_convention(self):
        # Accepting both at once would let a caller silently get the other one.
        with pytest.raises(ValueError) as caught:
            DepartureArea(name="ambiguous", splay_percent=15.0, splay_degrees=15.0)
        assert "exactly one" in str(caught.value)
        with pytest.raises(ValueError):
            DepartureArea(name="neither")

    def test_a_cap_limits_the_width(self):
        capped = DepartureArea(name="capped", splay_percent=15.0,
                               max_half_width_m=600.0)
        assert capped.half_width_at(100_000.0) == 600.0

    def test_containment_at_the_boundary_is_inclusive(self):
        at = 2.1 * METRES_PER_NM
        edge = Position(along_track_m=at, lateral_m=PANS_OPS_STRAIGHT.half_width_at(at))
        assert PANS_OPS_STRAIGHT.contains(edge)

    def test_an_obstacle_behind_the_der_is_never_inside(self):
        assert not PANS_OPS_STRAIGHT.contains(
            Position(along_track_m=-100.0, lateral_m=0.0)
        )

    def test_the_area_names_itself_in_its_description(self):
        # An answer that does not say which convention it used is not an answer.
        assert "15%" in PANS_OPS_STRAIGHT.describe()
        assert "15 deg" in OLS_INSTRUMENT_DEPARTURE.describe()


class TestAreaMembershipInAReview:
    def crane(self) -> Obstacle:
        return obstacle("CRANE-2291", feet=412, nm=2.1, bearing_from_der_deg=168)

    def far_off(self) -> Obstacle:
        return obstacle("MAST-09", feet=90 * 3.280839895, nm=3.0,
                        bearing_from_der_deg=120)

    def review(self, area=PANS_OPS_STRAIGHT):
        return review_runway(
            "RWY16", [self.crane(), self.far_off()],
            runway_bearing_deg=160, area=area,
        )

    def test_an_obstacle_in_the_area_is_inside(self):
        assert [o.identifier for o in self.review().inside_area] == ["CRANE-2291"]

    def test_an_obstacle_well_off_the_centreline_is_outside(self):
        assert [o.identifier for o in self.review().outside_area] == ["MAST-09"]

    def test_an_obstacle_outside_the_area_does_not_set_the_gradient(self):
        # Which is the point of having an area at all.
        assert self.review().governing.identifier == "CRANE-2291"

    def test_with_no_area_every_measurable_obstacle_counts(self):
        wide = review_runway("RWY16", [self.crane(), self.far_off()],
                             runway_bearing_deg=160)
        assert len(wide.assessable) == 2
        assert wide.contains(self.crane()) is None

    def test_membership_is_unknown_rather_than_false_without_a_position(self):
        # Not the same as outside, and must not print as though it were.
        vague = review_runway("RWY16", [obstacle("VAGUE")],
                              runway_bearing_deg=160, area=PANS_OPS_STRAIGHT)
        assert vague.contains(vague.obstacles[0]) is None

    def test_the_render_names_the_area_it_tested_against(self):
        printed = self.review().render()
        assert "PROTECTED AREA" in printed
        assert "PANS-OPS straight departure" in printed
        assert "INSIDE   CRANE-2291" in printed
        assert "outside  MAST-09" in printed

    def test_choosing_the_other_convention_can_change_the_answer(self):
        # An obstacle can be inside one and outside the other, which is exactly
        # why the convention is stated rather than assumed.
        edge = obstacle("EDGE", feet=300, nm=2.0, bearing_from_der_deg=173)
        narrow = review_runway("RWY16", [edge], runway_bearing_deg=160,
                               area=PANS_OPS_STRAIGHT)
        wide = review_runway("RWY16", [edge], runway_bearing_deg=160,
                             area=OLS_INSTRUMENT_DEPARTURE)
        assert narrow.contains(edge) is False
        assert wide.contains(edge) is True
