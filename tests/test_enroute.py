"""ENR 6 — the chart, drawn from ENR 3 rather than from an argument list.

The plan view could already draw a map. What it could not do was get the map
from the AIP: it was handed positions and airways by its caller, so the
drawing was as good as the arguments and no better. These assertions are about
the four ways getting it from ENR 3 could go wrong.

**The binding floor, not the friendliest one.** An airway is summarised across
every segment held for it, and the number that decides whether a level is
available end to end is the *highest* minimum on it. Reporting the lowest
would put a flight on an airway at a level available on part of it.

**A level filter is a filter, not a verdict.** Airways whose published band
excludes the level are set aside with a reason, never deleted and never drawn
as though they were an option.

**An airway with no band is never filtered out.** Not knowing the floor is not
the same as the floor being satisfied, and dropping it would make a coverage
gap look like a level restriction.

**No geometry is invented.** ENR 3 read without the ENR 4.4 coordinate table
draws no positions and says so.

Every designator, level and coordinate below is a fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aeropub.ats import (
    AtsStructure,
    CruisingLevels,
    PointKind,
    RouteSegment,
    SignificantPoint,
)
from aeropub.enroute import chart_for, chart_html, profile_for
from aeropub.entities import named
from aeropub.navaids import Navaid, NavaidKind, NavaidRegister
from aeropub.notam_register import (
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 3.1",
        retrieved_at=NOW,
        content_hash="c" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def point(designator: str, lat=None, lon=None, **overrides) -> SignificantPoint:
    fields = dict(
        designator=designator, source=ref(), latitude=lat, longitude=lon
    )
    fields.update(overrides)
    return SignificantPoint(**fields)


def seg(route: str, start: str, end: str, **overrides) -> RouteSegment:
    fields = dict(
        route=route, start=start, end=end, source=ref(), region="AAAA"
    )
    fields.update(overrides)
    return RouteSegment(**fields)


POINTS = (
    point("ALSEM", 26.4, 50.9),
    point("MIDLE", 28.9, 48.2),
    point("KUKLA", 31.6, 45.1),
    point("RASKI", 35.2, 40.4),
    point("VELOX", 38.0, 36.0),
    point("TOPRA", 41.2, 30.5),
    point("ZEBRA"),
)

SEGMENTS = (
    seg("UM688", "ALSEM", "MIDLE", mea_ft=24500, maa_ft=46000,
        navigation_spec="RNAV 5", controlling_unit="AAAA ACC"),
    seg("UM688", "MIDLE", "KUKLA", mea_ft=26000, maa_ft=46000,
        navigation_spec="RNAV 5", controlling_unit="AAAA ACC"),
    seg("L604", "KUKLA", "RASKI", mea_ft=9500, maa_ft=24500,
        direction=CruisingLevels.EVEN, navigation_spec="RNAV 5"),
    seg("N191", "RASKI", "VELOX", mea_ft=32000, navigation_spec="RNP 4",
        region="BBBB", controlling_unit="BBBB ACC"),
    seg("W12", "VELOX", "ZEBRA"),
    seg("W12", "ZEBRA", "TOPRA"),
)

STRUCTURE = AtsStructure(points=POINTS, segments=SEGMENTS)


def notam(identifier: str, entity: str, kind=SubjectKind.ROUTE) -> RegisteredNotam:
    return RegisteredNotam(
        identifier=identifier,
        subjects=(Subject(kind=kind, entity=entity),),
        effective_start=NOW - timedelta(days=1),
        effective_end=NOW + timedelta(days=2),
        source=ref(locator=identifier),
        text="test fixture",
    )


# --------------------------------------------------------------------------
# The airway profile
# --------------------------------------------------------------------------


class TestProfile:
    def test_the_binding_floor_is_the_highest_minimum_on_the_airway(self):
        """The lowest would be a level available on part of the airway and not
        on the rest, and it is the number a flight gets planned onto."""
        found = profile_for(STRUCTURE, "UM688")
        assert found.floor_ft == 26000

    def test_the_ceiling_is_the_lowest_maximum_for_the_same_reason(self):
        found = profile_for(STRUCTURE, "UM688")
        assert found.ceiling_ft == 46000

    def test_the_points_are_in_published_order(self):
        assert profile_for(STRUCTURE, "UM688").points == (
            "ALSEM", "MIDLE", "KUKLA",
        )

    def test_a_one_way_airway_says_so(self):
        found = profile_for(STRUCTURE, "L604")
        assert found.is_one_way
        assert "even levels only" in found.describe()

    def test_a_two_way_airway_does_not(self):
        assert not profile_for(STRUCTURE, "UM688").is_one_way

    def test_segments_with_no_floor_are_counted_not_ignored(self):
        found = profile_for(STRUCTURE, "W12")
        assert found.unbounded == 2
        assert not found.band_known

    def test_an_airway_nobody_holds_is_none_not_empty(self):
        """A coverage answer, not a statement that the airway does not
        exist."""
        assert profile_for(STRUCTURE, "Z99") is None

    def test_an_open_ended_band_says_which_end_is_open(self):
        text = profile_for(STRUCTURE, "N191").describe()
        assert "no maximum published" in text

    def test_the_navigation_spec_travels_with_the_airway(self):
        assert profile_for(STRUCTURE, "N191").navigation_specs == ("RNP 4",)

    def test_a_spec_that_changes_along_the_airway_is_not_flattened(self):
        """More than one is a finding, not a value to pick between."""
        held = AtsStructure(
            points=POINTS,
            segments=(
                seg("Q1", "ALSEM", "MIDLE", navigation_spec="RNAV 5"),
                seg("Q1", "MIDLE", "KUKLA", navigation_spec="RNP 4"),
            ),
        )
        assert profile_for(held, "Q1").navigation_specs == ("RNAV 5", "RNP 4")

    def test_a_direction_that_changes_along_the_airway_falls_back_to_both(self):
        held = AtsStructure(
            points=POINTS,
            segments=(
                seg("Q2", "ALSEM", "MIDLE", direction=CruisingLevels.EVEN),
                seg("Q2", "MIDLE", "KUKLA", direction=CruisingLevels.BOTH),
            ),
        )
        assert not profile_for(held, "Q2").is_one_way


class TestAdmits:
    def test_a_level_inside_the_band_is_admitted(self):
        assert profile_for(STRUCTURE, "UM688").admits(35000) is True

    def test_a_level_below_the_binding_floor_is_not(self):
        assert profile_for(STRUCTURE, "UM688").admits(25000) is False

    def test_a_level_above_the_ceiling_is_not(self):
        assert profile_for(STRUCTURE, "L604").admits(35000) is False

    def test_an_airway_with_no_band_answers_neither(self):
        """Not permission and not refusal."""
        assert profile_for(STRUCTURE, "W12").admits(35000) is None

    def test_exactly_the_floor_is_admitted(self):
        assert profile_for(STRUCTURE, "UM688").admits(26000) is True


# --------------------------------------------------------------------------
# The chart
# --------------------------------------------------------------------------


class TestChart:
    def test_every_airway_held_is_drawn_when_no_level_is_given(self):
        found = chart_for(STRUCTURE)
        assert {p.route for p in found.profiles} == {
            "UM688", "L604", "N191", "W12",
        }

    def test_the_positions_come_from_the_structure_itself(self):
        """Not from the caller. That is the whole point of this module."""
        found = chart_for(STRUCTURE)
        drawn = {p.designator for p in found.view.points}
        assert "ALSEM" in drawn and "TOPRA" in drawn

    def test_a_point_with_no_published_position_is_listed_not_placed(self):
        found = chart_for(STRUCTURE)
        assert "ZEBRA" in found.view.unplottable
        assert "ZEBRA" not in {p.designator for p in found.view.points}

    def test_an_airway_short_of_its_points_is_a_different_shape(self):
        found = chart_for(STRUCTURE)
        w12 = next(a for a in found.view.airways if a.route == "W12")
        assert w12.gaps == 1
        assert not found.is_conclusive

    def test_a_structure_with_no_coordinates_draws_no_geometry(self):
        """ENR 3 read without the ENR 4.4 table. It says so rather than
        drawing something."""
        bare = AtsStructure(
            points=(point("ALSEM"), point("MIDLE")),
            segments=(seg("UM688", "ALSEM", "MIDLE"),),
        )
        found = chart_for(bare)
        assert found.view.bounds is None
        assert set(found.view.unplottable) == {"ALSEM", "MIDLE"}
        assert "coverage gap" in chart_html(found)

    def test_the_chart_can_be_scoped_to_one_region(self):
        found = chart_for(STRUCTURE, regions=["BBBB"])
        assert [p.route for p in found.profiles] == ["N191"]

    def test_the_chart_can_be_scoped_to_named_airways(self):
        found = chart_for(STRUCTURE, routes=["L604"])
        assert [p.route for p in found.profiles] == ["L604"]

    def test_an_unknown_region_draws_nothing_rather_than_everything(self):
        assert chart_for(STRUCTURE, regions=["ZZZZ"]).profiles == ()

    def test_the_title_names_the_region_when_one_is_scoped(self):
        assert "BBBB" in chart_for(STRUCTURE, regions=["BBBB"]).view.title


class TestLevelFilter:
    def test_an_airway_whose_band_excludes_the_level_is_set_aside(self):
        found = chart_for(STRUCTURE, level_ft=35000)
        assert [r for r, _why in found.excluded] == ["L604"]
        assert "L604" not in {p.route for p in found.profiles}

    def test_the_reason_names_the_published_number(self):
        found = chart_for(STRUCTURE, level_ft=35000)
        assert "24500" in found.excluded[0][1]

    def test_an_airway_below_the_level_is_excluded_for_its_floor(self):
        found = chart_for(STRUCTURE, level_ft=10000)
        reasons = dict(found.excluded)
        assert "26000" in reasons["UM688"]

    def test_an_airway_with_no_band_is_never_filtered_out(self):
        """Dropping it would make a coverage gap look like a level
        restriction."""
        found = chart_for(STRUCTURE, level_ft=35000)
        assert "W12" in {p.route for p in found.profiles}
        assert found.unbanded == ("W12",)

    def test_the_render_says_what_it_set_aside_and_why(self):
        page = chart_for(STRUCTURE, level_ft=35000).render()
        assert "NOT AVAILABLE AT 35000 FT" in page
        assert "DRAWN WITHOUT A LEVEL CHECK" in page

    def test_no_level_filters_nothing(self):
        found = chart_for(STRUCTURE)
        assert found.excluded == ()
        assert found.unbanded == ()


class TestNotamsAndClosure:
    def test_a_closed_airway_is_drawn_as_closed(self):
        found = chart_for(STRUCTURE, closed_routes=["UM688"])
        airway = next(a for a in found.view.airways if a.route == "UM688")
        assert airway.closed

    def test_a_notam_against_an_airway_reaches_it(self):
        register = NotamRegister(notams=(notam("A0044/26", named("ATS", "L604")),))
        found = chart_for(STRUCTURE, notams=register, at=NOW)
        airway = next(a for a in found.view.airways if a.route == "L604")
        assert airway.notams == 1

    def test_a_notam_against_a_fix_reaches_the_point(self):
        register = NotamRegister(
            notams=(notam("A0045/26", "FIX:KUKLA", SubjectKind.NAVAID),)
        )
        found = chart_for(STRUCTURE, notams=register, at=NOW)
        marked = next(p for p in found.view.points if p.designator == "KUKLA")
        assert marked.notams == 1

    def test_no_notam_register_marks_nothing(self):
        found = chart_for(STRUCTURE)
        assert all(a.notams == 0 for a in found.view.airways)


class TestDetail:
    def test_every_drawn_airway_carries_its_published_numbers(self):
        """An airway on a chart with no numbers against it is a line."""
        found = chart_for(STRUCTURE)
        airway = next(a for a in found.view.airways if a.route == "UM688")
        assert "26000" in airway.detail
        assert "RNAV 5" in airway.detail

    def test_a_one_way_airway_is_marked_for_the_drawing(self):
        found = chart_for(STRUCTURE)
        airway = next(a for a in found.view.airways if a.route == "L604")
        assert airway.one_way

    def test_the_detail_reaches_the_svg_as_a_click_target(self):
        page = chart_html(chart_for(STRUCTURE))
        assert "pv-airway-group" in page
        assert "RNAV 5" in page

    def test_navaid_positions_fill_in_what_the_structure_lacks(self):
        aids = NavaidRegister(
            navaids=(
                Navaid(
                    ident="ZEBRA",
                    kind=NavaidKind.VOR_DME,
                    source=ref(locator="ENR 4.1"),
                    latitude=44.0,
                    longitude=26.0,
                    frequency_mhz=113.9,
                ),
            )
        )
        found = chart_for(STRUCTURE, navaids=aids)
        assert "ZEBRA" not in found.view.unplottable
        marked = next(p for p in found.view.points if p.designator == "ZEBRA")
        assert marked.kind == "navaid"

    def test_a_structure_position_wins_over_a_navaid_one(self):
        """ENR 4.4 is the route structure's own coordinate table. Where both
        publish a point, the one the structure was read with is used, so the
        drawing matches the airway it came from."""
        aids = NavaidRegister(
            navaids=(
                Navaid(
                    ident="ALSEM",
                    kind=NavaidKind.VOR,
                    source=ref(locator="ENR 4.1"),
                    latitude=1.0,
                    longitude=1.0,
                ),
            )
        )
        found = chart_for(STRUCTURE, navaids=aids)
        drawn = next(p for p in found.view.points if p.designator == "ALSEM")
        assert drawn.position.latitude == 26.4
