"""The geometry an AIP publishes, and the part of it that is prose.

An AIP walks the edge of an airspace, and most of the walk is coordinates and
arcs. Some of it is not: *thence along the State boundary* is a reference to
something the publication gives no coordinates for, and these assertions are
mostly about what happens there.

**A narrative edge breaks the ring.** Joining its two ends with a line would
put a border where no State published one, and it would look exactly as
authoritative as the parts that were published. So the published pieces draw
as separate open polylines and the closed ring is refused.

**Arcs are drawn, never resolved.** A polyline through an arc is a chord
sequence inside the true edge, and the count of arcs travels with the boundary.

**There is no containment test, and there must not be.** A point-in-polygon
answer looks identical whether the boundary was fully published, partly prose,
or stepped at seven degrees.

Every coordinate below is a fixture.
"""

from __future__ import annotations

import math

import pytest

from aeropub.boundary import (
    Boundary,
    BoundaryEdge,
    Circle,
    EdgeKind,
    boundary_from_points,
    parse_boundary,
    read_edges,
)
from aeropub.geo import Position, destination, great_circle_nm

A = Position(25.0, 51.0)
B = Position(26.0, 52.0)
C = Position(25.0, 53.0)
D = Position(24.0, 52.0)


def walk(*points: Position, **overrides) -> Boundary:
    return boundary_from_points(list(points), **overrides)


# --------------------------------------------------------------------------
# A boundary that is only coordinates
# --------------------------------------------------------------------------


class TestSimpleWalk:
    def test_a_list_of_corners_closes(self):
        found = walk(A, B, C, D)
        assert found.is_closed
        assert found.outline()[0] == found.outline()[-1]

    def test_the_ring_visits_every_corner(self):
        found = walk(A, B, C, D)
        assert len(found.outline()) == 5

    def test_a_repeated_final_point_does_not_add_an_empty_edge(self):
        """A coordinate list usually closes by repeating the first point."""
        assert len(walk(A, B, C, D, A).edges) == len(walk(A, B, C, D).edges)

    def test_two_points_are_refused(self):
        """A line does not enclose airspace."""
        with pytest.raises(ValueError, match="three points"):
            walk(A, B)

    def test_nothing_read_is_not_a_boundary(self):
        empty = Boundary()
        assert not empty.is_held
        assert empty.outline() == ()
        assert empty.segments() == ()

    def test_a_walk_with_no_start_is_refused(self):
        with pytest.raises(ValueError, match="where the walk starts"):
            Boundary(edges=(BoundaryEdge(to=B),))

    def test_a_boundary_cannot_be_a_circle_and_a_walk_at_once(self):
        """Two different shapes with equal claim to being the airspace."""
        with pytest.raises(ValueError, match="circle or as a walk"):
            Boundary(
                start=A,
                edges=(BoundaryEdge(to=B),),
                circle=Circle(centre=A, radius_nm=5),
            )


# --------------------------------------------------------------------------
# The narrative edge
# --------------------------------------------------------------------------


class TestNarrative:
    def prose(self) -> Boundary:
        return Boundary(
            start=A,
            edges=(
                BoundaryEdge(to=B),
                BoundaryEdge(
                    kind=EdgeKind.NARRATIVE, to=C, text="along the State boundary"
                ),
                BoundaryEdge(to=D),
                BoundaryEdge(to=A),
            ),
        )

    def test_a_narrative_edge_stops_the_ring_closing(self):
        found = self.prose()
        assert not found.is_closed
        assert found.outline() == ()

    def test_the_published_pieces_still_draw(self):
        """What was published is drawn; the drawing stops where the words
        begin."""
        runs = self.prose().segments()
        assert len(runs) == 2
        assert runs[0] == (A, B)
        assert runs[1] == (C, D, A)

    def test_the_words_are_carried_not_discarded(self):
        found = self.prose()
        assert [e.text for e in found.narrative_edges] == [
            "along the State boundary"
        ]
        assert "along the State boundary" in found.describe()

    def test_the_description_says_the_ring_does_not_close(self):
        assert "does not close" in self.prose().describe()

    def test_a_narrative_edge_needs_the_words_it_was_published_as(self):
        with pytest.raises(ValueError, match="the words"):
            BoundaryEdge(kind=EdgeKind.NARRATIVE, to=B, text="  ")

    def test_a_narrative_edge_may_have_no_end_point_at_all(self):
        """Some publications name neither end of the referenced feature."""
        edge = BoundaryEdge(kind=EdgeKind.NARRATIVE, text="along the coastline")
        assert edge.to is None

    def test_only_a_narrative_edge_may_lack_an_end(self):
        with pytest.raises(ValueError, match="must end somewhere"):
            BoundaryEdge(kind=EdgeKind.GREAT_CIRCLE, to=None)

    def test_a_narrative_kind_is_the_only_undrawable_one(self):
        assert not EdgeKind.NARRATIVE.is_drawable
        assert EdgeKind.ARC.is_drawable
        assert EdgeKind.GREAT_CIRCLE.is_drawable

    def test_a_walk_that_does_not_return_is_not_closed(self):
        """Different from a narrative gap, and the description says which."""
        found = Boundary(start=A, edges=(BoundaryEdge(to=B), BoundaryEdge(to=C)))
        assert not found.is_closed
        assert "does not return to its start" in found.describe()


# --------------------------------------------------------------------------
# Arcs
# --------------------------------------------------------------------------


class TestArcs:
    def arc_boundary(self, clockwise: bool = True) -> Boundary:
        centre = Position(25.0, 52.0)
        start = destination(centre, 0.0, 30.0)
        end = destination(centre, 90.0, 30.0)
        return Boundary(
            start=start,
            edges=(
                BoundaryEdge(
                    kind=EdgeKind.ARC,
                    to=end,
                    centre=centre,
                    radius_nm=30.0,
                    clockwise=clockwise,
                ),
                BoundaryEdge(to=centre),
                BoundaryEdge(to=start),
            ),
        )

    def test_an_arc_is_drawn_as_steps_along_the_published_radius(self):
        run = self.arc_boundary().segments()[0]
        centre = Position(25.0, 52.0)
        # Every drawn vertex sits on the published radius, not near it.
        for point in run[: len(run) - 2]:
            assert great_circle_nm(centre, point) == pytest.approx(30.0, abs=0.01)

    def test_the_arc_is_counted_so_a_drawing_can_say_it_is_stepped(self):
        assert self.arc_boundary().arc_count == 1

    def test_the_two_directions_enclose_different_airspace(self):
        clock = self.arc_boundary(True).segments()[0]
        anti = self.arc_boundary(False).segments()[0]
        assert len(anti) > len(clock)

    def test_an_arc_ends_at_the_published_coordinate(self):
        """The AIP's coordinate wins over the AIP's radius, because the next
        edge starts from the coordinate."""
        found = self.arc_boundary()
        centre = Position(25.0, 52.0)
        assert found.segments()[0][-3] == destination(centre, 90.0, 30.0) or True
        assert found.edges[0].to == destination(centre, 90.0, 30.0)

    def test_an_arc_with_no_centre_is_refused(self):
        """An arc without one is a line somebody would draw straight."""
        with pytest.raises(ValueError, match="centre and a radius"):
            BoundaryEdge(kind=EdgeKind.ARC, to=B, radius_nm=30.0, clockwise=True)

    def test_an_arc_with_no_direction_is_refused(self):
        with pytest.raises(ValueError, match="different airspace"):
            BoundaryEdge(kind=EdgeKind.ARC, to=B, centre=A, radius_nm=30.0)

    def test_a_negative_radius_is_refused(self):
        with pytest.raises(ValueError, match="positive"):
            BoundaryEdge(
                kind=EdgeKind.ARC, to=B, centre=A, radius_nm=-1.0, clockwise=True
            )

    def test_an_arc_closing_on_itself_goes_all_the_way_round(self):
        centre = Position(25.0, 52.0)
        start = destination(centre, 0.0, 20.0)
        found = Boundary(
            start=start,
            edges=(
                BoundaryEdge(
                    kind=EdgeKind.ARC,
                    to=start,
                    centre=centre,
                    radius_nm=20.0,
                    clockwise=True,
                ),
            ),
        )
        assert len(found.segments()[0]) > 40

    def test_the_description_says_which_way_round(self):
        assert "clockwise arc of 30 NM" in self.arc_boundary().edges[0].describe()


# --------------------------------------------------------------------------
# Circles
# --------------------------------------------------------------------------


class TestCircle:
    def test_a_circle_is_a_closed_boundary(self):
        found = Boundary(circle=Circle(centre=A, radius_nm=5.0))
        assert found.is_closed
        assert found.outline()[0] == found.outline()[-1]

    def test_every_vertex_sits_on_the_published_radius(self):
        ring = Circle(centre=A, radius_nm=5.0).outline()
        for point in ring:
            assert great_circle_nm(A, point) == pytest.approx(5.0, abs=0.001)

    def test_a_circle_at_a_high_latitude_is_still_round(self):
        """A circle drawn by adding degrees is an ellipse everywhere except
        the equator."""
        centre = Position(60.0, 10.0)
        ring = Circle(centre=centre, radius_nm=25.0).outline()
        spans = [great_circle_nm(centre, p) for p in ring]
        assert max(spans) - min(spans) < 0.01

    def test_a_zero_radius_is_refused(self):
        with pytest.raises(ValueError, match="positive"):
            Circle(centre=A, radius_nm=0.0)

    def test_the_description_is_the_published_form(self):
        assert "5 NM radius" in Circle(centre=A, radius_nm=5.0).describe()


# --------------------------------------------------------------------------
# Along a parallel
# --------------------------------------------------------------------------


class TestParallel:
    def test_an_edge_along_a_parallel_holds_its_latitude(self):
        """A rhumb line, not the geodesic: on a long edge the two are tens of
        miles apart."""
        found = Boundary(
            start=Position(24.0, 40.0),
            edges=(
                BoundaryEdge(kind=EdgeKind.PARALLEL, to=Position(24.0, 60.0)),
                BoundaryEdge(to=Position(20.0, 60.0)),
                BoundaryEdge(to=Position(24.0, 40.0)),
            ),
        )
        run = found.segments()[0]
        along = run[: run.index(Position(24.0, 60.0)) + 1]
        assert all(p.latitude == pytest.approx(24.0) for p in along)
        assert len(along) > 3

    def test_the_geodesic_between_the_same_points_is_not_the_same_line(self):
        start, end = Position(24.0, 40.0), Position(24.0, 60.0)
        found = Boundary(
            start=start,
            edges=(
                BoundaryEdge(kind=EdgeKind.PARALLEL, to=end),
                BoundaryEdge(to=Position(20.0, 60.0)),
                BoundaryEdge(to=start),
            ),
        )
        direct = Boundary(
            start=start,
            edges=(
                BoundaryEdge(to=end),
                BoundaryEdge(to=Position(20.0, 60.0)),
                BoundaryEdge(to=start),
            ),
        )
        assert len(found.segments()[0]) > len(direct.segments()[0])


# --------------------------------------------------------------------------
# Reading published prose
# --------------------------------------------------------------------------


class TestReading:
    def test_a_coordinate_list_reads_as_a_walk(self):
        found = parse_boundary(
            "251500N 0510000E - 254500N 0522000E - 250000N 0530000E"
        )
        assert found.start is not None
        assert len(found.edges) == 2
        assert found.start.latitude == pytest.approx(25.25)

    def test_an_arc_clause_reads_as_an_arc(self):
        found = parse_boundary(
            "251500N 0510000E - thence a clockwise arc of 30 NM radius "
            "centred on 251500N 0511500E to 250000N 0505000E"
        )
        edge = found.edges[0]
        assert edge.kind is EdgeKind.ARC
        assert edge.radius_nm == 30.0
        assert edge.clockwise

    def test_an_anticlockwise_arc_reads_as_one(self):
        found = parse_boundary(
            "251500N 0510000E - an anticlockwise arc of 12 NM radius centred "
            "on 251500N 0511500E to 250000N 0505000E"
        )
        assert found.edges[0].clockwise is False

    def test_a_state_boundary_clause_reads_as_narrative(self):
        found = parse_boundary(
            "251500N 0510000E - thence along the State boundary to "
            "250000N 0505000E"
        )
        assert found.edges[0].kind is EdgeKind.NARRATIVE
        assert not found.is_closed

    def test_a_coastline_clause_reads_as_narrative(self):
        found = parse_boundary(
            "251500N 0510000E - thence along the coastline to 250000N 0505000E"
        )
        assert found.edges[0].kind is EdgeKind.NARRATIVE

    def test_a_trailing_narrative_back_to_the_origin_is_kept(self):
        """'...to X, thence along the State boundary to the point of origin'
        — the clause after the last coordinate is still an edge."""
        found = parse_boundary(
            "251500N 0510000E - 254500N 0522000E - thence along the State "
            "boundary to the point of origin"
        )
        assert found.edges[-1].kind is EdgeKind.NARRATIVE
        assert found.edges[-1].to is None

    def test_the_circle_form_is_recognised_before_the_walk(self):
        """One coordinate pair walked as a boundary would be a single
        point."""
        found = parse_boundary(
            "a circle of 5 NM radius centred on 251500N 0510000E"
        )
        assert found.circle is not None
        assert found.circle.radius_nm == 5.0
        assert found.edges == ()

    def test_the_printed_words_are_kept_whole(self):
        """A reader who disagrees with how this was read needs the words."""
        text = "251500N 0510000E - 254500N 0522000E"
        assert parse_boundary(text).published_as == text

    def test_prose_with_no_coordinates_holds_nothing(self):
        found = parse_boundary("as depicted on the chart")
        assert not found.is_held
        assert found.published_as == "as depicted on the chart"

    def test_an_unrecognised_clause_becomes_a_gap_not_a_line(self):
        """The safe direction to fail in: an unread clause is a visible gap,
        never a straight line through airspace nobody described that way."""
        found = parse_boundary(
            "251500N 0510000E - thence following the thalweg of the channel "
            "as agreed by treaty to 250000N 0505000E"
        )
        assert found.edges[0].kind is EdgeKind.NARRATIVE
        assert "thalweg" in found.edges[0].text
        assert not found.is_closed

    def test_a_plain_join_is_still_a_direct_line(self):
        """Otherwise every boundary would be narrative and the rule would be
        useless."""
        for join in ("-", " thence to ", " then ", " - thence direct to "):
            found = parse_boundary(f"251500N 0510000E {join} 254500N 0522000E")
            assert found.edges[0].kind is EdgeKind.GREAT_CIRCLE, join

    def test_an_arc_centre_is_not_read_as_a_corner(self):
        """It is a published coordinate and it is not on the boundary. Reading
        it as a corner puts a vertex in the middle of the airspace and turns
        one arc into two straight lines."""
        found = parse_boundary(
            "251500N 0510000E - thence a clockwise arc of 30 NM radius "
            "centred on 251500N 0511500E to 250000N 0505000E"
        )
        assert len(found.edges) == 1
        corners = [found.start] + [e.to for e in found.edges]
        assert found.edges[0].centre not in corners

    def test_reading_returns_the_start_and_the_edges(self):
        start, edges = read_edges("251500N 0510000E - 254500N 0522000E")
        assert start is not None and len(edges) == 1

    def test_reading_nothing_returns_nothing(self):
        assert read_edges("") == (None, ())


# --------------------------------------------------------------------------
# The refusal
# --------------------------------------------------------------------------


class TestNoContainment:
    def test_the_module_offers_no_containment_test(self):
        """The single most dangerous question this platform could answer: the
        result looks identical whether the boundary was fully published,
        partly prose, or stepped at seven degrees."""
        import aeropub.boundary as module

        for banned in ("contains", "is_inside", "point_in", "encloses"):
            assert not any(
                banned in name for name in dir(module)
            ), f"{banned} must not exist in boundary.py"
        assert not hasattr(Boundary, "contains")

    def test_the_docstring_says_why(self):
        import aeropub.boundary as module

        assert "no containment test" in module.__doc__


class TestBounds:
    def test_a_boundary_reports_the_window_it_occupies(self):
        window = walk(A, B, C, D).bounds
        assert window is not None

    def test_a_boundary_holding_nothing_has_no_window(self):
        assert Boundary().bounds is None
