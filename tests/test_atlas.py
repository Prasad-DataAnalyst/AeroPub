"""Four ENR sections on one sheet, and the four ways that becomes a lie.

**An area whose edge is partly prose is never filled.** A filled shape reads
as a definite extent, and the extent is exactly what "thence along the State
boundary" withheld. It draws as the published pieces, dashed and open.

**Nothing is placed at a position nobody published.** A volume with no
boundary read, a point with no coordinate: listed under the map, never on it.

**Which State comes from the AIP, not from the map.** The document that
published the section is the answer. The coastline layer knows about countries
and is never asked.

**There is still no containment test.** Putting a route and an FIR on the same
sheet does not make one contain the other, and a containment answer from a
boundary that is partly prose and stepped through its arcs is the most
dangerous thing this platform could emit.

Every designator and coordinate below is a fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aeropub.airspace import Airspace, AirspaceClass, AirspaceStructure, AirspaceType
from aeropub.atlas import atlas_html, atlas_svg, build_atlas
from aeropub.ats import AtsStructure, CruisingLevels, RouteSegment, SignificantPoint
from aeropub.boundary import Boundary, BoundaryEdge, Circle, EdgeKind, boundary_from_points
from aeropub.entities import named
from aeropub.geo import Position
from aeropub.hazards import Activation, Hazard, HazardKind, HazardRegister
from aeropub.navaids import Navaid, NavaidKind, NavaidRegister
from aeropub.notam_register import (
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def ref(document: str = "AIP AA ENR 2.1", **overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document=document,
        locator="row 1",
        retrieved_at=NOW,
        content_hash="d" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


P = {
    "ALSEM": Position(26.4, 50.9),
    "MIDLE": Position(28.9, 48.2),
    "KUKLA": Position(31.6, 45.1),
    "RASKI": Position(35.2, 40.4),
    "ZEBRA": None,
}

FIR_RING = [
    Position(24.0, 46.0),
    Position(36.0, 46.0),
    Position(36.0, 54.0),
    Position(24.0, 54.0),
]


def fir(**overrides) -> Airspace:
    fields = dict(
        designator="AAAA",
        kind=AirspaceType.FIR,
        source=ref(),
        airspace_class=AirspaceClass.A,
        lower_ft=24500.0,
        upper_ft=66000.0,
        unit="Alpha Control",
        frequency_mhz=127.5,
        boundary=boundary_from_points(FIR_RING),
    )
    fields.update(overrides)
    return Airspace(**fields)


def tma(**overrides) -> Airspace:
    fields = dict(
        designator="ALPHA TMA",
        kind=AirspaceType.TMA,
        source=ref(document="AIP AA ENR 2.1"),
        airspace_class=AirspaceClass.C,
        region="AAAA",
        lower_ft=2500.0,
        upper_ft=24500.0,
        boundary=Boundary(circle=Circle(centre=Position(26.0, 51.0), radius_nm=30.0)),
    )
    fields.update(overrides)
    return Airspace(**fields)


def danger(**overrides) -> Hazard:
    fields = dict(
        designator="AD-31",
        kind=HazardKind.DANGER,
        source=ref(document="AIP AA ENR 5.1"),
        region="AAAA",
        lower_ft=0.0,
        upper_ft=15000.0,
        activation=Activation.BY_NOTAM,
        activity="gunnery",
        authority="Alpha CAA",
        boundary=Boundary(circle=Circle(centre=Position(29.0, 49.5), radius_nm=12.0)),
    )
    fields.update(overrides)
    return Hazard(**fields)


def point(designator: str) -> SignificantPoint:
    held = P[designator]
    return SignificantPoint(
        designator=designator,
        source=ref(document="AIP AA ENR 4.4"),
        latitude=held.latitude if held else None,
        longitude=held.longitude if held else None,
    )


def seg(route: str, a: str, b: str, **overrides) -> RouteSegment:
    fields = dict(
        route=route,
        start=a,
        end=b,
        source=ref(document="AIP AA ENR 3.2"),
        region="AAAA",
        mea_ft=24500.0,
        maa_ft=46000.0,
        navigation_spec="RNAV 5",
        controlling_unit="Alpha Control",
    )
    fields.update(overrides)
    return RouteSegment(**fields)


STRUCTURE = AtsStructure(
    points=tuple(point(d) for d in P),
    segments=(
        seg("UM688", "ALSEM", "MIDLE"),
        seg("UM688", "MIDLE", "KUKLA"),
        seg("L604", "KUKLA", "RASKI", mea_ft=9500.0, maa_ft=24500.0,
            direction=CruisingLevels.EVEN),
        seg("W12", "RASKI", "ZEBRA", mea_ft=None, maa_ft=None),
    ),
)

AIRSPACE = AirspaceStructure(volumes=(fir(), tma()))
HAZARDS = HazardRegister(hazards=(danger(),))
NAVAIDS = NavaidRegister(
    navaids=(
        Navaid(
            ident="ALP",
            kind=NavaidKind.VOR_DME,
            source=ref(document="AIP AA ENR 4.1"),
            latitude=25.6,
            longitude=51.2,
            frequency_mhz=113.9,
        ),
    )
)


def atlas(**overrides):
    fields = dict(
        airspace=AIRSPACE, structure=STRUCTURE, navaids=NAVAIDS, hazards=HAZARDS
    )
    fields.update(overrides)
    return build_atlas(**fields)


def notam(identifier: str, entity: str, kind=SubjectKind.AIRSPACE) -> RegisteredNotam:
    return RegisteredNotam(
        identifier=identifier,
        subjects=(Subject(kind=kind, entity=entity),),
        effective_start=NOW - timedelta(days=1),
        effective_end=NOW + timedelta(days=2),
        source=ref(),
        text="fixture",
    )


# --------------------------------------------------------------------------
# What ends up on the sheet
# --------------------------------------------------------------------------


class TestAssembly:
    def test_all_four_sections_are_present(self):
        found = atlas()
        assert found.firs and found.terminals and found.hazards
        assert found.routes and found.points

    def test_the_fir_carries_what_enr_2_publishes(self):
        detail = atlas().firs[0].detail
        assert "Class A" in detail
        assert "Alpha Control" in detail
        assert "127.500" in detail

    def test_the_hazard_carries_what_enr_5_publishes(self):
        detail = atlas().hazards[0].detail
        assert "gunnery" in detail
        assert "Alpha CAA" in detail
        assert "by notam" in detail

    def test_the_route_carries_what_enr_3_publishes(self):
        route = next(r for r in atlas().routes if r.designator == "UM688")
        assert "RNAV 5" in route.detail
        assert "24500" in route.detail

    def test_a_point_says_which_airways_run_through_it(self):
        """The interchange question, and the one a chart exists to answer."""
        found = atlas()
        kukla = next(p for p in found.points if p.designator == "KUKLA")
        assert set(kukla.routes) == {"UM688", "L604"}

    def test_a_navaid_is_drawn_even_where_no_route_uses_it(self):
        """What an en-route chart shows."""
        found = atlas()
        assert any(p.designator == "ALP" for p in found.points)

    def test_a_name_in_a_table_that_no_route_uses_is_not_drawn(self):
        """A chart of every name in a national table is a chart of nothing."""
        held = AtsStructure(
            points=(point("ALSEM"), point("MIDLE"), point("KUKLA")),
            segments=(seg("UM688", "ALSEM", "MIDLE"),),
        )
        found = build_atlas(structure=held)
        assert not any(p.designator == "KUKLA" for p in found.points)

    def test_a_section_not_supplied_contributes_nothing(self):
        found = build_atlas(structure=STRUCTURE)
        assert found.firs == () and found.hazards == ()
        assert found.routes


class TestWhichState:
    def test_every_feature_says_which_document_published_it(self):
        """The answer to "whose airspace is this". Never the country under the
        point — that is geography, and an FIR is not a country."""
        found = atlas()
        assert found.firs[0].published_in == "AIP AA ENR 2.1"
        assert found.hazards[0].published_in == "AIP AA ENR 5.1"
        assert next(
            r for r in found.routes if r.designator == "UM688"
        ).published_in == "AIP AA ENR 3.2"
        assert next(
            p for p in found.points if p.designator == "ALSEM"
        ).published_in == "AIP AA ENR 4.4"

    def test_the_source_reaches_the_drawing(self):
        assert "AIP AA ENR 2.1" in atlas_svg(atlas())

    def test_the_panel_shows_the_source(self):
        assert "'source'" in atlas_html(atlas()) or "source" in atlas_html(atlas())


# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------


class TestRefusals:
    def test_a_volume_with_no_boundary_is_listed_not_drawn(self):
        held = AirspaceStructure(volumes=(fir(boundary=None), tma()))
        found = build_atlas(airspace=held)
        assert not any(a.designator == "AAAA" for a in found.areas)
        assert any("AAAA" in u for u in found.unplaced)

    def test_a_point_with_no_position_is_listed_not_placed(self):
        found = atlas()
        assert any("ZEBRA" in u for u in found.unplaced)
        assert not any(p.designator == "ZEBRA" for p in found.points)

    def test_a_route_short_of_its_points_says_so(self):
        found = atlas()
        w12 = next((r for r in found.routes if r.designator == "W12"), None)
        assert w12 is not None and w12.gaps == 1

    def test_an_edge_partly_in_words_is_not_filled(self):
        """A filled shape reads as a definite extent, and the extent is
        exactly what the prose withheld."""
        prose = Boundary(
            start=FIR_RING[0],
            edges=(
                BoundaryEdge(to=FIR_RING[1]),
                BoundaryEdge(
                    kind=EdgeKind.NARRATIVE,
                    to=FIR_RING[2],
                    text="along the State boundary",
                ),
                BoundaryEdge(to=FIR_RING[3]),
                BoundaryEdge(to=FIR_RING[0]),
            ),
        )
        found = build_atlas(airspace=AirspaceStructure(volumes=(fir(boundary=prose),)))
        area = found.firs[0]
        assert not area.closed and area.narrative == 1
        svg = atlas_svg(found)
        assert "at-fir-open" in svg
        assert "<polygon" not in svg.split('data-layer="fir"')[1].split("</g>")[0]

    def test_the_page_says_which_edges_are_partly_words(self):
        prose = Boundary(
            start=FIR_RING[0],
            edges=(
                BoundaryEdge(to=FIR_RING[1]),
                BoundaryEdge(
                    kind=EdgeKind.NARRATIVE, to=FIR_RING[2], text="along the coast"
                ),
                BoundaryEdge(to=FIR_RING[3]),
                BoundaryEdge(to=FIR_RING[0]),
            ),
        )
        page = atlas_html(
            build_atlas(airspace=AirspaceStructure(volumes=(fir(boundary=prose),)))
        )
        assert "partly described in words" in page
        assert "definite extent" in page

    def test_a_full_looking_picture_is_not_complete(self):
        assert not atlas().is_complete

    def test_everything_held_and_closed_is_complete(self):
        held = AtsStructure(
            points=(point("ALSEM"), point("MIDLE")),
            segments=(seg("UM688", "ALSEM", "MIDLE"),),
        )
        found = build_atlas(airspace=AirspaceStructure(volumes=(fir(),)), structure=held)
        assert found.is_complete

    def test_nothing_held_says_it_is_a_coverage_gap(self):
        found = build_atlas()
        assert found.bounds is None
        svg = atlas_svg(found)
        assert "coverage gap" in svg
        assert "not empty airspace" in svg


class TestNoContainment:
    def test_the_atlas_offers_no_containment_test(self):
        import aeropub.atlas as module

        for banned in ("contains", "inside", "point_in", "encloses", "which_fir"):
            assert not any(banned in name.lower() for name in dir(module))

    def test_the_page_says_so_in_words(self):
        page = atlas_html(atlas())
        assert "does not make one contain the other" in page

    def test_the_page_says_a_border_is_not_an_fir_boundary(self):
        assert "not evidence about" in atlas_html(atlas())


# --------------------------------------------------------------------------
# Scoping and NOTAM
# --------------------------------------------------------------------------


class TestScoping:
    def test_a_level_sets_aside_the_routes_that_cannot_take_it(self):
        found = atlas(level_ft=35000.0)
        assert not any(r.designator == "L604" for r in found.routes)
        assert any(r.designator == "UM688" for r in found.routes)

    def test_a_level_sets_aside_the_volumes_that_cannot_reach_it(self):
        found = atlas(level_ft=45000.0)
        assert not any(a.designator == "ALPHA TMA" for a in found.areas)

    def test_a_route_with_no_band_is_never_set_aside_by_level(self):
        """Not knowing the floor is not the same as the floor being
        satisfied."""
        found = atlas(level_ft=35000.0)
        assert any(r.designator == "W12" for r in found.routes)

    def test_naming_a_region_narrows_the_sheet(self):
        found = atlas(regions=["AAAA"])
        assert found.firs and found.terminals

    def test_an_unknown_region_draws_nothing_rather_than_everything(self):
        found = atlas(regions=["ZZZZ"])
        assert found.areas == () and found.routes == ()


class TestNotams:
    def test_a_notam_on_a_region_reaches_the_area(self):
        register = NotamRegister(notams=(notam("A0001/26", named("AIRSPACE", "AAAA")),))
        found = atlas(notams=register, at=NOW)
        assert found.firs[0].notams == 1

    def test_a_notam_on_an_airway_reaches_the_route(self):
        register = NotamRegister(
            notams=(notam("A0002/26", named("ATS", "L604"), SubjectKind.ROUTE),)
        )
        found = atlas(notams=register, at=NOW)
        assert next(r for r in found.routes if r.designator == "L604").notams == 1

    def test_a_notam_on_a_fix_reaches_the_point(self):
        register = NotamRegister(
            notams=(notam("A0003/26", "FIX:KUKLA", SubjectKind.NAVAID),)
        )
        found = atlas(notams=register, at=NOW)
        assert next(p for p in found.points if p.designator == "KUKLA").notams == 1

    def test_no_register_marks_nothing(self):
        found = atlas()
        assert all(a.notams == 0 for a in found.areas)


# --------------------------------------------------------------------------
# The drawing
# --------------------------------------------------------------------------


class TestDrawing:
    def test_the_geography_is_drawn_underneath(self):
        svg = atlas_svg(atlas())
        assert svg.index('data-layer="basemap"') < svg.index('data-layer="fir"')

    def test_only_the_geography_near_the_window_is_carried(self):
        found = atlas()
        assert 0 < found.basemap.vertices < 7000

    def test_every_layer_can_be_switched(self):
        page = atlas_html(atlas())
        for layer in ("basemap", "fir", "terminal", "routes", "points", "hazard"):
            assert f'data-layer="{layer}"' in page

    def test_every_feature_carries_its_detail_for_the_panel(self):
        svg = atlas_svg(atlas())
        start = svg.index("data-info=") + len('data-info="')
        payload = svg[start : svg.index('"', start)]
        parsed = json.loads(
            payload.replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        assert {"name", "kind", "position", "detail", "published"} <= set(parsed)

    def test_a_designator_with_markup_cannot_break_the_drawing(self):
        held = AirspaceStructure(volumes=(fir(designator="A&B"),))
        assert "A&amp;B" in atlas_svg(build_atlas(airspace=held))

    def test_the_page_is_self_contained(self):
        page = atlas_html(atlas())
        assert "https://" not in page
        assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")

    def test_both_themes_are_defined(self):
        page = atlas_html(atlas())
        assert "prefers-color-scheme: dark" in page
        assert "--at-ink" in page

    def test_the_render_names_what_it_could_not_draw(self):
        text = atlas().render()
        assert "NAMED AND NOT DRAWN" in text
        assert "ZEBRA" in text
