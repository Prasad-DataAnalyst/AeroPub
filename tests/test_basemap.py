"""Geography, and the questions it is not allowed to answer.

This is the only module here that is not reading an aeronautical publication,
and the risk it introduces is obvious: a map with countries on it invites
somebody to decide which country an airspace is in by looking. The AIP answers
that — the State that published the section is the State whose airspace it is —
and a national border is not evidence about a flight information region
boundary. FIRs run over the high seas and are delegated between States; the two
lines disagree in a great many places.

So these assert what the layer is, that it is bounded in size, that it clips to
a window correctly, and that it offers nothing that would answer an
aeronautical question.
"""

from __future__ import annotations

import json

import pytest

from aeropub.basemap import (
    ATTRIBUTION,
    NOT_AERONAUTICAL,
    Basemap,
    attribution,
    borders,
    coastline,
    load_basemap,
)
from aeropub.geo import Bounds, Position, bounds_of, mercator, unmercator


class TestLoading:
    def test_the_vendored_data_loads(self):
        found = load_basemap()
        assert found.coastline and found.borders

    def test_it_is_the_whole_world(self):
        lats = [p.latitude for line in coastline() for p in line]
        lons = [p.longitude for line in coastline() for p in line]
        assert min(lats) < -60 and max(lats) > 70
        assert min(lons) < -170 and max(lons) > 170

    def test_it_is_small_enough_to_embed_in_a_page(self):
        """A chart carrying a coastline is no use if the coastline is the
        page."""
        assert load_basemap().vertices < 12000

    def test_every_page_carrying_it_can_say_where_it_came_from(self):
        assert "Natural Earth" in attribution()
        assert "public domain" in ATTRIBUTION

    def test_coordinates_are_read_in_geojson_order(self):
        """GeoJSON is longitude first. Reading it latitude first puts Qatar in
        the Indian Ocean."""
        for line in coastline()[:20]:
            for point in line[:20]:
                assert -90.0 <= point.latitude <= 90.0
                assert -180.0 <= point.longitude <= 180.0


class TestClipping:
    def gulf(self) -> Bounds:
        return bounds_of([Position(22.0, 45.0), Position(32.0, 57.0)])

    def test_a_window_keeps_only_what_reaches_it(self):
        whole = load_basemap()
        near = whole.clipped(self.gulf())
        assert 0 < near.vertices < whole.vertices

    def test_no_window_keeps_everything(self):
        assert load_basemap().clipped(None).vertices == load_basemap().vertices

    def test_the_window_is_read_as_projection_not_degrees(self):
        """Bounds is Mercator: x is longitude over 180 and y is the Mercator
        ordinate. Comparing its y against a latitude is a category error, and
        this module made it once."""
        far_north = bounds_of([Position(60.0, 5.0), Position(70.0, 30.0)])
        gulf = self.gulf()
        # In *projected* terms the northern window's y is far above the Gulf's.
        assert far_north.min_y > gulf.max_y
        # And clipping to it keeps different lines, which it would not if the
        # comparison were being made in the wrong space.
        north_lines = load_basemap().clipped(far_north).coastline
        gulf_lines = load_basemap().clipped(gulf).coastline
        assert north_lines != gulf_lines

    def test_a_line_reaching_the_window_is_kept_entire(self):
        """Cutting it would create endpoints that look like coastline and are
        not."""
        whole = load_basemap()
        near = whole.clipped(self.gulf())
        for line in near.coastline:
            assert line in whole.coastline

    def test_clipping_keeps_the_attribution(self):
        """It travels with the data, not with the code path that reduced it."""
        whole = load_basemap()
        assert whole.clipped(self.gulf()).attribution == whole.attribution
        assert "public domain" in whole.attribution

    def test_an_empty_basemap_clips_to_nothing_without_failing(self):
        assert Basemap().clipped(self.gulf()).vertices == 0


class TestProjectionRoundTrip:
    @pytest.mark.parametrize(
        "position",
        [
            Position(25.2731, 51.6081),
            Position(-33.87, 151.21),
            Position(60.0, -10.0),
            Position(0.0, 0.0),
        ],
    )
    def test_a_position_survives_projection_and_back(self, position):
        x, y = mercator(position)
        back = unmercator(x, y)
        assert back.latitude == pytest.approx(position.latitude, abs=1e-9)
        assert back.longitude == pytest.approx(position.longitude, abs=1e-9)


class TestItAnswersNothing:
    def test_it_offers_no_way_to_ask_which_country_a_point_is_in(self):
        """The AIP answers that: the State that published the section is the
        State whose airspace it is."""
        import aeropub.basemap as module

        for banned in ("country_at", "state_for", "contains", "which_country"):
            assert not hasattr(module, banned)
            assert not hasattr(Basemap, banned)

    def test_the_module_says_a_border_is_not_an_fir_boundary(self):
        import aeropub.basemap as module

        assert "not evidence about" in NOT_AERONAUTICAL
        assert "not an aeronautical source" in module.__doc__

    def test_the_data_file_carries_the_same_warning(self):
        """It travels with the bytes, not only with the code that reads
        them."""
        from aeropub.basemap import BASEMAP_PATH

        payload = json.loads(BASEMAP_PATH.read_text(encoding="utf-8"))
        assert "public domain" in payload["attribution"]
        assert "not an aeronautical source" in payload["not_aeronautical"].lower()
        assert "which State" in payload["not_aeronautical"]
