"""The plan view — a map that must not draw what nobody published.

The schematic could not lie about position because it claimed none. This one
can, so the assertions are about the two places it would:

**A point with no held position is never placed.** It is listed by name under
the drawing. A waypoint at a guessed position is the one output worse than no
drawing at all — a gap announces itself and a wrong position does not.

**An airway missing points is a different shape.** Drawing it through the five
points held out of seven produces a plausible line that is not the published
airway, so the missing count travels with it and the page says so.

The third thing tested is the track. A leg is drawn as a great circle through
intermediate positions computed on the sphere, because the straight line
between two points on a Mercator sheet is a different route.

Every position below is a fixture. None is a claim about a real waypoint.
"""

from __future__ import annotations

import json

import pytest

from aeropub.geo import Position, great_circle_nm
from aeropub.planview import PlanView, plan_html, plan_svg, plan_view

HELD = {
    "OTHH": Position(25.2731, 51.6081),
    "ALSEM": Position(26.4, 50.9),
    "MIDLE": Position(28.9, 48.2),
    "KUKLA": Position(31.6, 45.1),
    "RASKI": Position(35.2, 40.4),
    "EGLL": Position(51.4775, -0.4614),
    "ALP": Position(25.6, 51.2),
}


def view(**overrides) -> PlanView:
    fields = dict(
        positions=HELD,
        route_points=["OTHH", "ALSEM", "MIDLE", "KUKLA", "EGLL"],
        airways={"UM688": ["ALSEM", "MIDLE", "KUKLA"]},
        navaids=["ALP"],
        aerodromes=["OTHH", "EGLL"],
        title="OTHH-EGLL",
    )
    fields.update(overrides)
    return plan_view(**fields)


# --------------------------------------------------------------------------
# What is not drawn
# --------------------------------------------------------------------------


class TestUnplottable:
    def test_a_point_with_no_position_is_listed_not_placed(self):
        """A waypoint at a guessed position is the one output worse than no
        drawing at all."""
        found = view(route_points=["OTHH", "NOWHERE", "EGLL"])
        assert "NOWHERE" in found.unplottable
        assert "NOWHERE" not in {p.designator for p in found.points}

    def test_the_page_names_every_one_of_them(self):
        page = plan_html(view(route_points=["OTHH", "NOWHERE", "EGLL"]))
        assert "NOWHERE" in page
        assert "named and not drawn" in page

    def test_an_airway_through_an_unheld_point_reports_the_gap(self):
        """An airway missing points is a different shape from the published
        one."""
        found = view(airways={"N191": ["MIDLE", "RASKI", "ZEBRA"]})
        airway = found.airways[0]
        assert airway.gaps == 1
        assert len(airway.positions) == 2
        assert "different shape" in plan_html(found)

    def test_a_gap_anywhere_makes_the_view_incomplete(self):
        """Never true merely because the drawing looks full."""
        assert not view(airways={"N191": ["MIDLE", "ZEBRA"]}).is_complete

    def test_everything_held_is_complete(self):
        assert view().is_complete

    def test_a_leg_to_an_unheld_point_is_not_drawn(self):
        found = view(route_points=["OTHH", "NOWHERE"])
        assert not found.legs[0].is_drawable

    def test_nothing_held_says_it_is_a_coverage_gap(self):
        """Not an empty sky."""
        empty = plan_view(positions={}, route_points=["OTHH"])
        assert empty.bounds is None
        svg = plan_svg(empty)
        assert "coverage gap" in svg
        assert "not an empty sky" in svg


# --------------------------------------------------------------------------
# The track
# --------------------------------------------------------------------------


class TestTrack:
    def test_a_leg_is_drawn_through_intermediate_positions(self):
        """A straight line between two points on a flat projection is a
        different route."""
        leg = view().legs[0]
        assert len(leg.path) > 2

    def test_the_path_starts_and_ends_at_the_named_points(self):
        leg = view().legs[0]
        assert leg.path[0].latitude == pytest.approx(HELD["OTHH"].latitude)
        assert leg.path[-1].latitude == pytest.approx(HELD["ALSEM"].latitude)

    def test_each_leg_carries_its_computed_distance_and_initial_bearing(self):
        leg = view().legs[0]
        assert leg.distance_nm == pytest.approx(
            great_circle_nm(HELD["OTHH"], HELD["ALSEM"])
        )
        assert 0 < leg.bearing_deg <= 360

    def test_the_route_length_is_the_sum_of_the_legs(self):
        found = view()
        assert found.route_distance_nm == pytest.approx(
            sum(leg.distance_nm for leg in found.legs)
        )

    def test_a_partial_route_reports_no_length_at_all(self):
        """A partial total is a smaller number than the route, and a reader
        would take it for the route length."""
        assert view(route_points=["OTHH", "NOWHERE", "EGLL"]).route_distance_nm is None

    def test_the_page_says_the_length_is_computed_not_published(self):
        page = plan_html(view())
        assert "computed from the published" in page
        assert "Not a published distance" in page


# --------------------------------------------------------------------------
# The drawing
# --------------------------------------------------------------------------


class TestDrawing:
    def test_aerodromes_navaids_and_fixes_are_drawn_differently(self):
        found = view()
        kinds = {p.designator: p.kind for p in found.points}
        assert kinds["OTHH"] == "aerodrome"
        assert kinds["ALP"] == "navaid"
        assert kinds["ALSEM"] == "fix"

    def test_points_on_the_route_are_marked_as_such(self):
        found = view()
        assert next(p for p in found.points if p.designator == "ALSEM").on_route
        assert not next(p for p in found.points if p.designator == "ALP").on_route

    def test_a_closed_airway_is_drawn_as_closed(self):
        found = view(closed_routes=["UM688"])
        assert found.airways[0].closed
        svg = plan_svg(found)
        assert "pv-shut" in svg
        assert "CLOSED" in svg

    def test_a_notam_on_a_point_is_marked(self):
        found = view(notams=[("FIX:KUKLA", None, None)])
        assert next(p for p in found.points if p.designator == "KUKLA").notams == 1
        assert "pv-ring" in plan_svg(found)

    def test_a_notam_on_an_airway_reaches_the_airway(self):
        found = view(notams=[("ATS:UM688", None, None)])
        assert found.airways[0].notams == 1

    def test_every_point_carries_its_detail_for_the_panel(self):
        svg = plan_svg(view())
        assert "data-info=" in svg
        # The payload has to survive being an attribute and then being parsed.
        start = svg.index("data-info=") + len('data-info="')
        payload = svg[start : svg.index('"', start)]
        parsed = json.loads(
            payload.replace("&quot;", '"').replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
        )
        assert "position" in parsed and "name" in parsed

    def test_the_aspect_is_preserved_rather_than_stretched(self):
        """Stretching a projection to fill a box changes every angle on it,
        which is the one property Mercator is chosen for."""
        wide = plan_svg(view(), width=1200, height=400)
        tall = plan_svg(view(), width=400, height=1200)
        assert 'viewBox="0 0 1200 400"' in wide
        assert 'viewBox="0 0 400 1200"' in tall

    def test_a_designator_with_markup_cannot_break_the_drawing(self):
        found = plan_view(
            positions={"A&B": Position(0.0, 0.0)}, route_points=["A&B"]
        )
        assert "A&amp;B" in plan_svg(found)

    def test_the_drawing_carries_an_accessible_label(self):
        assert 'role="img"' in plan_svg(view())
        assert 'tabindex="0"' in plan_svg(view())


class TestPage:
    def test_the_page_refuses_the_chart_claim(self):
        page = plan_html(view())
        assert "not a chart" in page
        assert "no terrain" in page

    def test_the_page_is_self_contained(self):
        """No library, no runtime, no network."""
        page = plan_html(view())
        assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in page

    def test_the_layers_can_be_switched(self):
        page = plan_html(view())
        for layer in ("route", "airways", "points"):
            assert f'data-layer="{layer}"' in page

    def test_both_themes_are_defined(self):
        page = plan_html(view())
        assert "prefers-color-scheme: dark" in page
        assert "--pv-ink" in page
