"""Coordinates, and the arithmetic that has to be done on a sphere.

Three things carry this module and each is tested against the failure it
prevents.

**A coordinate parser that guesses places a waypoint somewhere nobody
published.** A point drawn in the wrong place is worse than a point not drawn:
one is a gap and the other is a map. So prose is refused, out-of-range figures
are refused, and a longitude offered where a latitude was expected is refused —
that last is the transposition that puts a fix in a different hemisphere.

**A straight line on a flat projection is a different route, not a simpler
one.** The great circle between two distant points sits far from the straight
line between them, so the midpoint is checked against the naive average rather
than against itself.

**Computed is not published.** The distance between two published coordinates
is a derived figure and never a substitute for the segment distance a State
prints. What it is good for is the disagreement, which means one of the two is
wrong.

The positions below are fixtures. None is a claim about a real waypoint.
"""

from __future__ import annotations

import math

import pytest

from aeropub.geo import (
    EARTH_RADIUS_NM,
    Bounds,
    CoordinateError,
    Position,
    bounds_of,
    distance_disagreement,
    format_coordinate,
    great_circle_nm,
    great_circle_path,
    initial_bearing,
    intermediate,
    mercator,
    parse_coordinate,
    parse_position,
)

#: Two positions far enough apart that a straight line and a great circle
#: visibly differ.
NEAR = Position(25.2731, 51.6081)
FAR = Position(51.4775, -0.4614)


# --------------------------------------------------------------------------
# Reading what an AIP prints
# --------------------------------------------------------------------------


class TestParsing:
    def test_the_forms_a_coordinate_column_prints(self):
        assert parse_coordinate("251530N") == pytest.approx(25.258333, abs=1e-6)
        assert parse_coordinate("0513015E") == pytest.approx(51.504167, abs=1e-6)
        assert parse_coordinate("2515N") == pytest.approx(25.25)
        assert parse_coordinate("25°15'30\"N") == pytest.approx(25.258333, abs=1e-6)
        assert parse_coordinate("-25.2583") == pytest.approx(-25.2583)

    def test_southern_and_western_hemispheres_are_negative(self):
        assert parse_coordinate("251530S") < 0
        assert parse_coordinate("0513015W") < 0

    def test_decimal_seconds_are_read(self):
        assert parse_coordinate("512230.5S") == pytest.approx(-51.375139, abs=1e-6)

    def test_prose_is_refused_rather_than_guessed(self):
        """A guessed coordinate is a point drawn in the wrong place."""
        for text in ("as depicted", "along the FIR boundary", "thence clockwise"):
            with pytest.raises(CoordinateError, match="could not be read"):
                parse_coordinate(text)

    def test_minutes_and_seconds_past_fifty_nine_are_refused(self):
        with pytest.raises(CoordinateError):
            parse_coordinate("257030N")

    def test_a_latitude_past_ninety_is_refused(self):
        with pytest.raises(CoordinateError):
            parse_coordinate("951530N")

    def test_a_longitude_offered_as_a_latitude_is_refused(self):
        """The transposition that puts a fix in a different hemisphere."""
        with pytest.raises(CoordinateError, match="longitude where a latitude"):
            parse_coordinate("0513015E", is_latitude=True)

    def test_a_latitude_offered_as_a_longitude_is_refused(self):
        with pytest.raises(CoordinateError, match="latitude where a longitude"):
            parse_coordinate("251530N", is_latitude=False)

    def test_nothing_is_refused_as_nothing(self):
        with pytest.raises(CoordinateError, match="no coordinate"):
            parse_coordinate("")
        with pytest.raises(CoordinateError):
            parse_coordinate(None)

    def test_a_pair_checks_each_half_as_the_half_it_should_be(self):
        found = parse_position("251530N", "0513015E")
        assert found.latitude == pytest.approx(25.258333, abs=1e-6)
        with pytest.raises(CoordinateError):
            parse_position("0513015E", "251530N")

    def test_a_boolean_is_not_a_coordinate(self):
        with pytest.raises(CoordinateError):
            parse_coordinate(True)


class TestFormatting:
    def test_it_prints_the_way_a_chart_does(self):
        assert format_coordinate(25.2731, is_latitude=True).endswith("N")
        assert len(format_coordinate(25.2731, is_latitude=True)) == 7
        assert len(format_coordinate(51.6081, is_latitude=False)) == 8

    def test_a_western_longitude_carries_its_letter(self):
        assert format_coordinate(-0.4614, is_latitude=False).endswith("W")

    def test_rounding_carries_rather_than_printing_sixty(self):
        """59.9995 seconds is a whole minute, and 5960 is not a coordinate."""
        printed = format_coordinate(25.0 + 59.9999 / 60.0, is_latitude=True)
        assert "60" not in printed[2:6]

    def test_it_round_trips_through_the_parser(self):
        for value in (25.2731, -51.375, 0.0, 89.9):
            printed = format_coordinate(value, is_latitude=True)
            assert parse_coordinate(printed, is_latitude=True) == pytest.approx(
                value, abs=1 / 3600.0
            )


class TestPosition:
    def test_an_impossible_latitude_is_refused_rather_than_clamped(self):
        """A clamped position would be drawn somewhere real."""
        with pytest.raises(ValueError, match="cannot exist"):
            Position(latitude=91.0, longitude=0.0)

    def test_an_impossible_longitude_is_refused(self):
        with pytest.raises(ValueError, match="longitude"):
            Position(latitude=0.0, longitude=181.0)

    def test_a_position_describes_itself_as_a_chart_would(self):
        assert NEAR.describe().count("N") + NEAR.describe().count("E") == 2


# --------------------------------------------------------------------------
# The sphere
# --------------------------------------------------------------------------


class TestGreatCircle:
    def test_the_distance_to_itself_is_nothing(self):
        assert great_circle_nm(NEAR, NEAR) == pytest.approx(0.0, abs=1e-9)

    def test_a_degree_of_latitude_is_about_sixty_miles(self):
        """The one distance every airman knows, and the check that the radius
        and the units agree."""
        found = great_circle_nm(Position(0.0, 0.0), Position(1.0, 0.0))
        assert found == pytest.approx(60.0, abs=0.5)

    def test_the_distance_is_symmetric(self):
        assert great_circle_nm(NEAR, FAR) == pytest.approx(great_circle_nm(FAR, NEAR))

    def test_half_the_world_is_half_the_circumference(self):
        found = great_circle_nm(Position(0.0, 0.0), Position(0.0, 180.0))
        assert found == pytest.approx(math.pi * EARTH_RADIUS_NM, rel=1e-9)

    def test_bearing_due_north_is_three_sixty_not_zero(self):
        """The aviation convention, the same one the holding module keeps."""
        assert initial_bearing(Position(0.0, 0.0), Position(1.0, 0.0)) == 360.0

    def test_bearing_due_east_is_ninety(self):
        assert initial_bearing(Position(0.0, 0.0), Position(0.0, 1.0)) == pytest.approx(
            90.0, abs=0.01
        )

    def test_the_bearing_changes_along_a_great_circle(self):
        """Which is why it is called the *initial* bearing."""
        start = initial_bearing(NEAR, FAR)
        halfway = initial_bearing(intermediate(NEAR, FAR, 0.5), FAR)
        assert abs(start - halfway) > 5.0


class TestIntermediate:
    def test_the_midpoint_is_not_the_average_of_the_numbers(self):
        """Averaging latitudes on a long leg puts the midpoint far off track."""
        midpoint = intermediate(NEAR, FAR, 0.5)
        naive = (NEAR.latitude + FAR.latitude) / 2.0
        assert abs(midpoint.latitude - naive) > 2.0

    def test_the_ends_are_the_ends(self):
        assert intermediate(NEAR, FAR, 0.0).latitude == pytest.approx(NEAR.latitude)
        assert intermediate(NEAR, FAR, 1.0).latitude == pytest.approx(FAR.latitude)

    def test_the_midpoint_is_equidistant_from_both_ends(self):
        midpoint = intermediate(NEAR, FAR, 0.5)
        assert great_circle_nm(NEAR, midpoint) == pytest.approx(
            great_circle_nm(midpoint, FAR), rel=1e-6
        )

    def test_a_leg_across_the_antimeridian_does_not_go_the_long_way(self):
        """Averaging two longitudes across it puts the midpoint on the far
        side of the world."""
        midpoint = intermediate(Position(0.0, 179.0), Position(0.0, -179.0), 0.5)
        assert abs(midpoint.longitude) == pytest.approx(180.0, abs=0.001)

    def test_a_zero_length_leg_does_not_divide_by_zero(self):
        assert intermediate(NEAR, NEAR, 0.5) == NEAR

    def test_a_fraction_outside_the_leg_is_refused(self):
        with pytest.raises(ValueError):
            intermediate(NEAR, FAR, 1.5)


class TestPath:
    def test_the_path_starts_and_ends_where_the_leg_does(self):
        path = great_circle_path(NEAR, FAR, steps=8)
        assert len(path) == 9
        assert path[0].latitude == pytest.approx(NEAR.latitude)
        assert path[-1].latitude == pytest.approx(FAR.latitude)

    def test_the_path_bows_away_from_the_straight_line(self):
        """A drawing that used the straight line would show a path nobody
        flies."""
        path = great_circle_path(NEAR, FAR, steps=8)
        straight = [
            NEAR.latitude + (FAR.latitude - NEAR.latitude) * i / 8 for i in range(9)
        ]
        gaps = [abs(p.latitude - s) for p, s in zip(path, straight)]
        assert max(gaps) > 2.0

    def test_at_least_one_step_is_required(self):
        with pytest.raises(ValueError):
            great_circle_path(NEAR, FAR, steps=0)


# --------------------------------------------------------------------------
# Computed against published
# --------------------------------------------------------------------------


class TestDisagreement:
    def test_a_published_distance_close_to_the_coordinates_is_not_a_finding(self):
        computed = great_circle_nm(NEAR, FAR)
        assert distance_disagreement(round(computed), NEAR, FAR) is None

    def test_a_published_distance_far_from_the_coordinates_is(self):
        """One of the two is wrong and somebody should find out which."""
        found = distance_disagreement(120.0, NEAR, FAR)
        assert found is not None and found > 100

    def test_a_missing_side_produces_nothing(self):
        assert distance_disagreement(None, NEAR, FAR) is None
        assert distance_disagreement(100.0, None, FAR) is None
        assert distance_disagreement(100.0, NEAR, None) is None

    def test_the_tolerance_is_proportional_as_well_as_absolute(self):
        """States round, and a long segment rounds further than a short one."""
        near_far = Position(26.0, 52.0)
        computed = great_circle_nm(NEAR, near_far)
        assert distance_disagreement(computed + 1.5, NEAR, near_far) is None
        assert distance_disagreement(computed + 60, NEAR, near_far) is not None


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


class TestProjection:
    def test_the_origin_projects_to_the_origin(self):
        x, y = mercator(Position(0.0, 0.0))
        assert x == pytest.approx(0.0, abs=1e-12)
        assert y == pytest.approx(0.0, abs=1e-12)

    def test_east_is_positive_and_north_is_positive(self):
        assert mercator(Position(0.0, 90.0))[0] > 0
        assert mercator(Position(45.0, 0.0))[1] > 0

    def test_longitude_is_linear(self):
        """Mercator's whole point: meridians are evenly spaced."""
        a = mercator(Position(0.0, 30.0))[0]
        b = mercator(Position(0.0, 60.0))[0]
        assert b == pytest.approx(2 * a)

    def test_latitude_is_not_linear(self):
        """Which is why nothing measures anything off the projection."""
        a = mercator(Position(30.0, 0.0))[1]
        b = mercator(Position(60.0, 0.0))[1]
        assert b > 2 * a

    def test_the_poles_are_clamped_rather_than_sent_to_infinity(self):
        x, y = mercator(Position(90.0, 0.0))
        assert math.isfinite(y)

    def test_bounds_contain_every_position(self):
        found = bounds_of([NEAR, FAR])
        for position in (NEAR, FAR):
            x, y = mercator(position)
            assert found.min_x <= x <= found.max_x
            assert found.min_y <= y <= found.max_y

    def test_nothing_held_gives_no_window_rather_than_a_default_one(self):
        """A map drawn over an arbitrary extent shows empty ocean and reads as
        a place with nothing in it."""
        assert bounds_of([]) is None

    def test_a_single_position_still_gets_a_window_with_extent(self):
        """A window with no extent divides by zero on the first point drawn."""
        padded = bounds_of([NEAR]).padded()
        assert padded.width > 0
        assert padded.height > 0
