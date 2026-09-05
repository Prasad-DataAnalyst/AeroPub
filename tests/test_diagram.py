"""The route profile — a picture that must not flatter the data behind it.

A drawing is the easiest place in this platform to lie by accident. A tidy box
looks checked whether or not anything checked it; a blank strip looks clear
whether or not anybody read it. So the assertions here are almost entirely
about what a *gap* looks like:

- a direct leg and an unresolved leg are drawn as holes, never as low boxes,
  because a low box reads as a segment with a low minimum;
- a region nobody read is hatched, never blank;
- a leg with no published floor is neither passed nor failed, because an
  unchecked leg is not a passing one.

The other thing tested hard is the binding figure. A leg filed between two
points crosses several published segments, and the drawing must stand on the
*highest* floor among them. Drawing the first segment's minimum would show a
level as flyable on a leg where one segment in the middle forbids it — the
exact error the route screen exists to catch, reintroduced by the picture.

Nothing here asserts pixels. It asserts which class a shape is drawn in.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aeropub.ats import (
    AtsStructure,
    CruisingLevels,
    Resolution,
    RouteSegment,
    expand,
    parse_route_string,
)
from aeropub.diagram import (
    Band,
    RouteDiagram,
    diagram_for,
    network_for,
    network_html,
    network_svg,
    route_html,
    route_svg,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def ref() -> SourceRef:
    return SourceRef(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 3.1",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )


def segment(route: str, start: str, end: str, **overrides) -> RouteSegment:
    fields = dict(
        route=route, start=start, end=end, source=ref(),
        direction=CruisingLevels.ODD, distance_nm=100.0,
    )
    fields.update(overrides)
    return RouteSegment(**fields)


#: One filed leg crossing two published segments with different minimums. The
#: second is the binding one.
STRUCTURE = AtsStructure(segments=(
    segment("UM688", "ALSEM", "MIDLE", mea_ft=15000, distance_nm=120),
    segment("UM688", "MIDLE", "KUKLA", mea_ft=24000, distance_nm=80),
    segment("L604", "KUKLA", "RASKI", mea_ft=18000, maa_ft=46000, distance_nm=140),
))


def drawn(text="ALSEM UM688 KUKLA L604 RASKI", **overrides):
    expansion = expand(parse_route_string(text), STRUCTURE)
    fields = dict(planned_ft=20000)
    fields.update(overrides)
    return diagram_for(expansion, **fields)


# --------------------------------------------------------------------------
# The binding figure
# --------------------------------------------------------------------------


class TestBinding:
    def test_a_leg_stands_on_the_highest_floor_it_crosses(self):
        """Drawing the first segment's minimum would show a level as flyable
        on a leg where one segment in the middle forbids it."""
        band = drawn().bands[0]
        assert band.floor_ft == 24000

    def test_a_leg_takes_the_lowest_ceiling_it_crosses(self):
        band = drawn().bands[1]
        assert band.ceiling_ft == 46000

    def test_the_failing_leg_is_the_one_whose_floor_is_above_the_level(self):
        diagram = drawn()
        assert [b.via for b in diagram.failing] == ["UM688"]

    def test_a_level_above_a_published_ceiling_also_fails(self):
        diagram = drawn(planned_ft=48000)
        assert "L604" in [b.via for b in diagram.failing]

    def test_the_scale_always_contains_the_planned_level(self):
        """Nothing is ever drawn off the top of the plot."""
        assert drawn(planned_ft=51000).ceiling >= 51000

    def test_an_unlimited_ceiling_does_not_run_the_scale_away(self):
        band = Band(
            start="A", end="B", via="Q1", resolution=Resolution.RESOLVED,
            floor_ft=10000, ceiling_ft=float("inf"),
        )
        assert RouteDiagram(bands=(band,), planned_ft=20000).ceiling == 25000


# --------------------------------------------------------------------------
# What a gap looks like
# --------------------------------------------------------------------------


class TestGaps:
    def test_a_direct_leg_is_a_hole_and_not_a_low_box(self):
        """A low box reads as a segment with a low minimum."""
        band = drawn("ALSEM DCT KUKLA").bands[0]
        assert band.resolution is Resolution.DIRECT
        assert not band.is_drawn
        assert band.floor_ft is None

    def test_an_unresolved_leg_is_a_hole_too(self):
        band = drawn("ALSEM ZZ99 KUKLA").bands[0]
        assert band.resolution is Resolution.UNRESOLVED
        assert not band.is_drawn

    def test_a_leg_with_no_published_floor_is_neither_passed_nor_failed(self):
        """An unchecked leg is not a passing leg."""
        bare = AtsStructure(segments=(segment("Q1", "AAAAA", "BBBBB"),))
        expansion = expand(parse_route_string("AAAAA Q1 BBBBB"), bare)
        band = diagram_for(expansion, planned_ft=20000).bands[0]
        assert not band.is_drawn
        assert not band.fails(20000)

    def test_holes_are_counted_and_reported(self):
        diagram = drawn("ALSEM DCT KUKLA L604 RASKI")
        assert len(diagram.unchecked) == 1
        assert "gaps rather than" in route_html(diagram)

    def test_a_hole_is_drawn_hatched(self):
        assert 'fill="url(#rp-hatch)"' in route_svg(drawn("ALSEM DCT KUKLA"))

    def test_an_unread_region_is_hatched_and_not_blank(self):
        """Blank reads as clear."""
        diagram = drawn(regions=["OBBB", "LTAA"], unread_regions=["LTAA"])
        svg = route_svg(diagram)
        assert "rp-unread" in svg
        assert "not read" in svg
        assert "blank reads as clear" in route_html(diagram)


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------


class TestScale:
    def test_the_axis_is_to_distance_when_every_leg_publishes_one(self):
        assert drawn().to_scale
        assert "to published distance" in route_svg(drawn())

    def test_one_unmeasured_leg_drops_the_whole_axis_off_scale(self):
        """Part to scale and part not is a shape nobody published, and a
        reader has no way to tell which part is which."""
        diagram = drawn("ALSEM DCT KUKLA L604 RASKI")
        assert not diagram.to_scale
        assert "not to scale" in route_svg(diagram)

    def test_an_empty_route_still_draws_a_page(self):
        diagram = RouteDiagram()
        svg = route_svg(diagram)
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")


# --------------------------------------------------------------------------
# NOTAM
# --------------------------------------------------------------------------


class TestNotamMarks:
    def test_a_notam_on_a_waypoint_reaches_the_band(self):
        """Points are keyed by kind. Looking up the bare designator matched
        nothing and drew a clean band over a waypoint with a NOTAM on it."""
        diagram = drawn(notams=[("FIX:KUKLA", None, None)])
        assert diagram.bands[0].notams == 1

    def test_a_notam_on_an_airway_reaches_the_band(self):
        diagram = drawn(notams=[("ATS:L604", None, None)])
        assert diagram.bands[1].notams == 1

    def test_a_navaid_keyed_point_is_matched_too(self):
        diagram = drawn(notams=[("NAVAID:KUKLA", None, None)])
        assert diagram.bands[0].notams == 1

    def test_a_direct_leg_takes_no_airway_notam(self):
        diagram = drawn("ALSEM DCT KUKLA", notams=[("ATS:UM688", None, None)])
        assert diagram.bands[0].notams == 0

    def test_the_count_is_drawn(self):
        assert "1 NOTAM" in route_svg(drawn(notams=[("FIX:KUKLA", None, None)]))


# --------------------------------------------------------------------------
# The drawing itself
# --------------------------------------------------------------------------


class TestDrawing:
    def test_a_failing_leg_is_drawn_in_the_adverse_class(self):
        assert "rp-adverse" in route_svg(drawn())

    def test_a_clear_route_is_not(self):
        assert "rp-adverse" not in route_svg(drawn(planned_ft=35000))

    def test_the_planned_level_is_drawn_and_labelled(self):
        svg = route_svg(drawn())
        assert "rp-planned" in svg
        assert "planned FL200" in svg

    def test_every_waypoint_is_labelled(self):
        svg = route_svg(drawn())
        for point in ("ALSEM", "KUKLA", "RASKI"):
            assert f">{point}<" in svg

    def test_the_points_are_in_route_order_without_repeats(self):
        assert drawn().points == ("ALSEM", "KUKLA", "RASKI")

    def test_the_drawing_carries_an_accessible_label(self):
        assert 'role="img"' in route_svg(drawn(title="OTHH-EGLL"))
        assert "OTHH-EGLL" in route_svg(drawn(title="OTHH-EGLL"))

    def test_a_designator_with_markup_in_it_cannot_break_the_drawing(self):
        """Route data comes from published documents and hand-written
        manifests. A designator with an ampersand in it must not end the
        drawing halfway through."""
        band = Band(
            start="A&B", end='C"D', via="<E>",
            resolution=Resolution.RESOLVED, floor_ft=10000,
        )
        svg = route_svg(RouteDiagram(bands=(band,), title="R&D <route>"))
        assert "A&amp;B" in svg
        assert "C&quot;D" in svg
        assert "&lt;E&gt;" in svg
        assert "R&amp;D &lt;route&gt;" in svg
        assert "<E>" not in svg

    def test_the_page_states_which_legs_fail(self):
        assert "cannot be flown on" in route_html(drawn())

    def test_the_page_is_self_contained(self):
        """No library, no runtime, no network."""
        page = route_html(drawn())
        assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
        assert "<script" not in page

    def test_both_themes_are_defined(self):
        page = route_html(drawn())
        assert "prefers-color-scheme: dark" in page
        assert "--rp-ink" in page


# --------------------------------------------------------------------------
# The network schematic
# --------------------------------------------------------------------------


NETWORK = AtsStructure(segments=(
    segment("UM688", "ALSEM", "MIDLE", mea_ft=15000, region="ALFA FIR"),
    segment("UM688", "MIDLE", "KUKLA", mea_ft=24000, region="ALFA FIR"),
    segment("UM688", "KUKLA", "VELOX", mea_ft=22000, region="ALFA FIR"),
    segment("L604", "KUKLA", "RASKI", mea_ft=18000, region="BRAVO FIR"),
    segment("L604", "RASKI", "TOPRA", mea_ft=16000, region="BRAVO FIR"),
    segment("N191", "MIDLE", "RASKI", mea_ft=11000, region="BRAVO FIR"),
))


class TestStructureLookups:
    def test_the_points_on_an_airway_come_out_in_flown_order(self):
        """Sorting the designators would draw a route that goes back on
        itself."""
        assert NETWORK.points_on("UM688") == ("ALSEM", "MIDLE", "KUKLA", "VELOX")

    def test_an_airway_nobody_holds_has_no_points(self):
        assert NETWORK.points_on("ZZ99") == ()

    def test_a_point_reports_every_airway_through_it(self):
        assert NETWORK.routes_through("KUKLA") == ("L604", "UM688")

    def test_a_point_on_one_airway_reports_one(self):
        assert NETWORK.routes_through("ALSEM") == ("UM688",)

    def test_segments_can_be_found_by_region(self):
        assert {s.route for s in NETWORK.in_region("ALFA FIR")} == {"UM688"}

    def test_an_empty_query_returns_nothing_rather_than_everything(self):
        assert NETWORK.routes_through("") == ()
        assert NETWORK.in_region("  ") == ()
        assert NETWORK.on("") == ()


class TestNetwork:
    def test_every_airway_becomes_a_lane(self):
        found = network_for(NETWORK)
        assert {lane.route for lane in found.lanes} == {"UM688", "L604", "N191"}

    def test_a_lane_carries_its_region_and_its_lowest_minimum(self):
        lane = next(l for l in network_for(NETWORK).lanes if l.route == "UM688")
        assert lane.region == "ALFA FIR"
        assert lane.lowest_ft == 15000

    def test_a_lane_spanning_two_regions_names_neither(self):
        """Naming one would put half an airway in the wrong State's airspace."""
        spread = AtsStructure(segments=(
            segment("Q1", "AAAAA", "BBBBB", region="ALFA FIR"),
            segment("Q1", "BBBBB", "CCCCC", region="BRAVO FIR"),
        ))
        assert network_for(spread).lanes[0].region == ""

    def test_the_interchanges_are_the_points_on_more_than_one_airway(self):
        found = network_for(NETWORK)
        assert found.interchanges == ("KUKLA", "MIDLE", "RASKI")

    def test_a_closure_is_passed_in_and_never_inferred_from_a_notam(self):
        """A NOTAM against an airway may close it, may restrict a level band
        on it, or may say something else. Deciding which from the presence of
        a NOTAM would be reading a message this module has not read."""
        found = network_for(NETWORK, notams=[("ATS:L604", None, None)])
        lane = next(l for l in found.lanes if l.route == "L604")
        assert not lane.is_closed
        assert lane.notams == 1

    def test_a_stated_closure_is_drawn_as_closed(self):
        found = network_for(NETWORK, closed_routes=["L604"])
        assert [lane.route for lane in found.closed] == ["L604"]
        assert "rn-closed" in network_svg(found)
        assert "CLOSED" in network_svg(found)

    def test_a_closure_names_where_the_alternative_starts(self):
        """The question a planner has the moment a NOTAM shuts a route."""
        page = network_html(network_for(NETWORK, closed_routes=["L604"]))
        assert "L604 closed" in page
        assert "KUKLA" in page and "RASKI" in page
        assert "change airway without a direct leg" in page

    def test_the_filed_route_is_marked_inside_the_structure(self):
        found = network_for(NETWORK, highlight=["ALSEM", "MIDLE"])
        assert "rn-filed" in network_svg(found)

    def test_an_interchange_is_drawn_with_a_connector(self):
        assert "rn-interchange" in network_svg(network_for(NETWORK))

    def test_an_empty_structure_says_it_is_a_coverage_gap(self):
        """Nothing drawn must not read as an empty network."""
        svg = network_svg(network_for(AtsStructure()))
        assert "coverage gap" in svg
        assert svg.startswith("<svg") and svg.endswith("</svg>")

    def test_the_drawing_refuses_the_map_claim(self):
        svg = network_svg(network_for(NETWORK))
        assert "holds no coordinates" in svg
        assert "nothing here is a map" in svg

    def test_the_drawing_carries_an_accessible_label(self):
        assert 'role="img"' in network_svg(network_for(NETWORK, title="ALFA"))

    def test_both_themes_are_defined(self):
        page = network_html(network_for(NETWORK))
        assert "prefers-color-scheme: dark" in page
        assert "--rn-ink" in page
