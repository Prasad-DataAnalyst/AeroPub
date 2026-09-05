"""ENR 1.5 and 3.6 — holding, and the entry worked out in the descent.

The entry sector is the reason this module exists, so it is tested against the
ICAO geometry boundary by boundary rather than by a couple of easy cases. A
line through the fix at 70° to the inbound track gives a 70° teardrop sector on
the holding side, a 110° parallel sector on the other, and 180° of direct — and
a left-hand pattern is the mirror. Getting the mirror wrong sends an aeroplane
out of the protected area on the side nobody protected.

The second thing tested hard is the 5° zone of flexibility. ICAO accepts either
adjoining entry within five degrees of a boundary, so inside it there are two
right answers, and a tool printing one of them as *the* answer teaches a
precision the procedure does not have.

The third is which authority a speed was checked against. Some States apply a
table other than ICAO's; a screen that did not say which it used would look
equally confident either way.

Every fix below is a fixture. None of it is a claim about a real pattern.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.holding import (
    FLEXIBILITY_DEG,
    ICAO_HOLDING_SPEEDS,
    EntrySector,
    HoldingPattern,
    HoldingRegister,
    SpeedBasis,
    TurnDirection,
    entry_for,
    holding_template,
    load_holding,
    max_holding_speed_kt,
    screen_holding,
    standard_outbound_time_min,
)
from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 3.6",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def pattern(**overrides) -> HoldingPattern:
    fields = dict(
        fix="ALSEM", inbound_track_deg=360.0, turn=TurnDirection.RIGHT,
        source=ref(),
    )
    fields.update(overrides)
    return HoldingPattern(**fields)


# --------------------------------------------------------------------------
# The entry, boundary by boundary
# --------------------------------------------------------------------------


class TestEntrySectors:
    """Checked against the ICAO geometry, not against a couple of easy cases."""

    def test_the_three_sectors_of_a_right_hand_pattern(self):
        held = pattern(inbound_track_deg=360.0, turn=TurnDirection.RIGHT)
        for heading, sector in (
            (0, EntrySector.DIRECT),
            (90, EntrySector.DIRECT),
            (300, EntrySector.DIRECT),
            (130, EntrySector.TEARDROP),
            (170, EntrySector.TEARDROP),
            (200, EntrySector.PARALLEL),
            (280, EntrySector.PARALLEL),
        ):
            assert entry_for(held, heading).sector is sector, heading

    def test_the_three_sectors_of_a_left_hand_pattern_are_the_mirror(self):
        """Getting the mirror wrong sends an aeroplane out of the protected
        area on the side nobody protected."""
        held = pattern(inbound_track_deg=360.0, turn=TurnDirection.LEFT)
        for heading, sector in (
            (0, EntrySector.DIRECT),
            (90, EntrySector.DIRECT),
            (170, EntrySector.DIRECT),
            (200, EntrySector.TEARDROP),
            (240, EntrySector.TEARDROP),
            (260, EntrySector.PARALLEL),
            (350, EntrySector.PARALLEL),
        ):
            assert entry_for(held, heading).sector is sector, heading

    def test_the_teardrop_sector_is_seventy_degrees_wide(self):
        held = pattern()
        wide = [
            offset
            for offset in range(360)
            if entry_for(held, offset).sector is EntrySector.TEARDROP
        ]
        assert len(wide) == 70

    def test_the_parallel_sector_is_a_hundred_and_ten_degrees_wide(self):
        held = pattern()
        wide = [
            offset
            for offset in range(360)
            if entry_for(held, offset).sector is EntrySector.PARALLEL
        ]
        assert len(wide) == 110

    def test_the_direct_sector_is_a_half_circle(self):
        held = pattern()
        wide = [
            offset
            for offset in range(360)
            if entry_for(held, offset).sector is EntrySector.DIRECT
        ]
        assert len(wide) == 180

    def test_the_sectors_rotate_with_the_inbound_track(self):
        """A pattern on 090 is the same geometry turned ninety degrees."""
        north = pattern(inbound_track_deg=360.0)
        east = pattern(inbound_track_deg=90.0)
        for offset in range(0, 360, 7):
            assert (
                entry_for(north, offset).sector
                is entry_for(east, (offset + 90) % 360).sector
            ), offset

    def test_a_heading_past_north_is_normalised(self):
        assert entry_for(pattern(), 450).sector is entry_for(pattern(), 90).sector

    def test_north_is_three_sixty_and_not_zero(self):
        """Every chart and every clearance says 360. A module storing 0 would
        print 000° where the plate says 360°."""
        assert pattern(inbound_track_deg=360).inbound_track_deg == 360.0
        assert pattern(inbound_track_deg=0).inbound_track_deg == 360.0
        assert entry_for(pattern(), 0).heading_deg == 360.0
        assert "360°" in entry_for(pattern(), 360).describe()

    def test_every_heading_gets_exactly_one_sector(self):
        held = pattern()
        assert all(entry_for(held, h).sector is not None for h in range(360))


class TestFlexibility:
    """ICAO accepts either adjoining entry within five degrees of a boundary."""

    def test_a_heading_just_inside_a_boundary_offers_the_neighbour(self):
        held = pattern()
        found = entry_for(held, 111)
        assert found.sector is EntrySector.TEARDROP
        assert found.alternative is EntrySector.DIRECT
        assert found.is_boundary

    def test_a_heading_just_outside_a_boundary_offers_the_other_neighbour(self):
        found = entry_for(pattern(), 109)
        assert found.sector is EntrySector.DIRECT
        assert found.alternative is EntrySector.TEARDROP

    def test_a_heading_clear_of_every_boundary_offers_nothing(self):
        found = entry_for(pattern(), 90)
        assert found.alternative is None
        assert not found.is_boundary

    def test_the_zone_is_five_degrees_either_side(self):
        held = pattern()
        assert entry_for(held, 110 - FLEXIBILITY_DEG + 1).is_boundary
        assert not entry_for(held, 110 - FLEXIBILITY_DEG - 1).is_boundary

    def test_the_boundary_at_north_is_found_too(self):
        """The direct/parallel boundary on a left pattern sits at the inbound
        track itself, which is where an off-by-one in the wrap would show."""
        held = pattern(turn=TurnDirection.LEFT)
        assert entry_for(held, 359).is_boundary
        assert entry_for(held, 1).is_boundary

    def test_the_document_says_either_is_acceptable(self):
        assert "equally acceptable" in entry_for(pattern(), 111).describe()


# --------------------------------------------------------------------------
# The published construction rules
# --------------------------------------------------------------------------


class TestSpeeds:
    def test_the_bands_are_read_at_their_boundaries(self):
        assert max_holding_speed_kt(14000) == 230.0
        assert max_holding_speed_kt(14001) == 240.0
        assert max_holding_speed_kt(20000) == 240.0
        assert max_holding_speed_kt(20001) == 265.0
        assert max_holding_speed_kt(34000) == 265.0

    def test_above_the_bands_there_is_no_knots_figure(self):
        """PANS-OPS gives a Mach number, and converting it needs a
        temperature this platform does not hold."""
        assert max_holding_speed_kt(35000) is None

    def test_the_table_climbs(self):
        speeds = [s for _, s in ICAO_HOLDING_SPEEDS]
        assert speeds == sorted(speeds)

    def test_outbound_timing_changes_at_the_threshold(self):
        assert standard_outbound_time_min(14000) == 1.0
        assert standard_outbound_time_min(14001) == 1.5


class TestPattern:
    def test_a_published_limit_governs_over_the_standard(self):
        held = pattern(speed_limit_kt=210)
        assert held.speed_limit_at(10000) == (210.0, SpeedBasis.PUBLISHED)

    def test_the_standard_applies_where_nothing_is_published(self):
        assert pattern().speed_limit_at(10000) == (230.0, SpeedBasis.ICAO)

    def test_above_the_bands_no_limit_is_established(self):
        assert pattern().speed_limit_at(40000) == (None, SpeedBasis.NONE)

    def test_the_outbound_leg_falls_back_to_the_standard_timing(self):
        value, unit = pattern().outbound_at(20000)
        assert value == 1.5
        assert "default" in unit

    def test_a_published_distance_answers_in_miles_at_every_level(self):
        held = pattern(outbound_distance_nm=7)
        assert held.outbound_at(30000) == (7.0, "NM")

    def test_timing_and_distance_together_are_refused(self):
        """Two different outbound legs would both look published."""
        with pytest.raises(ValueError, match="timing or by distance"):
            pattern(outbound_time_min=1.0, outbound_distance_nm=7.0)

    def test_an_inverted_band_is_refused(self):
        with pytest.raises(ValueError, match="above maximum"):
            pattern(minimum_ft=20000, maximum_ft=8000)

    def test_a_band_nobody_read_permits_nothing_and_forbids_nothing(self):
        """Reporting it as permitted would be a clearance this platform is in
        no position to give."""
        assert pattern().permits(10000) is None

    def test_a_pattern_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            HoldingPattern(
                fix="ALSEM", inbound_track_deg=360, turn=TurnDirection.RIGHT,
                source=None,
            )


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


class TestScreen:
    def test_a_level_outside_the_band_is_blocking(self):
        held = pattern(minimum_ft=8000, maximum_ft=24000)
        found = screen_holding(held, level_ft=30000)
        assert [f.blocking for f in found] == [True]
        assert "outside the published band" in found[0].what

    def test_a_level_inside_the_band_raises_nothing(self):
        held = pattern(minimum_ft=8000, maximum_ft=24000)
        assert screen_holding(held, level_ft=10000) == ()

    def test_a_speed_above_the_published_limit_is_blocking(self):
        held = pattern(minimum_ft=0, maximum_ft=40000, speed_limit_kt=220)
        found = [
            f for f in screen_holding(held, level_ft=10000, speed_kt=250)
            if "above the holding speed" in f.what
        ]
        assert found and found[0].blocking
        assert "published" in found[0].detail

    def test_a_speed_checked_against_the_standard_says_so(self):
        held = pattern(minimum_ft=0, maximum_ft=40000)
        found = [
            f for f in screen_holding(held, level_ft=10000, speed_kt=250)
            if "above the holding speed" in f.what
        ]
        assert "icao" in found[0].detail

    def test_a_state_limit_below_the_standard_is_surfaced(self):
        """A crew planning from PANS-OPS alone would not know about it."""
        held = pattern(minimum_ft=0, maximum_ft=40000, speed_limit_kt=210)
        found = [
            f for f in screen_holding(held, level_ft=10000, speed_kt=200)
            if "below the standard" in f.what
        ]
        assert found and not found[0].blocking

    def test_left_turns_are_surfaced_and_are_not_blocking(self):
        """Flyable, and worth knowing: the protected side is mirrored."""
        held = pattern(turn=TurnDirection.LEFT, minimum_ft=0, maximum_ft=40000)
        found = [f for f in screen_holding(held, level_ft=10000) if "left" in f.what]
        assert found and not found[0].blocking

    def test_an_unread_band_is_reported_rather_than_passed(self):
        found = screen_holding(pattern(), level_ft=10000)
        assert any("band not held" in f.what for f in found)

    def test_no_level_given_means_the_level_was_not_looked_at(self):
        """Not the same as cleared."""
        held = pattern(minimum_ft=8000, maximum_ft=24000)
        assert screen_holding(held) == ()

    def test_above_the_bands_the_speed_check_says_why_it_stopped(self):
        held = pattern(minimum_ft=0, maximum_ft=45000)
        found = [
            f for f in screen_holding(held, level_ft=40000, speed_kt=280)
            if "not established" in f.what
        ]
        assert found and not found[0].blocking


class TestRegister:
    def test_one_fix_can_carry_more_than_one_pattern(self):
        """An en-route hold and a missed approach hold share a fix with
        different tracks and different bands."""
        held = HoldingRegister(patterns=(
            pattern(inbound_track_deg=360),
            pattern(inbound_track_deg=180, procedure="ILS 34L missed approach"),
        ))
        assert len(held.at_fix("ALSEM")) == 2

    def test_patterns_on_a_route_come_back_in_the_order_of_the_fixes(self):
        held = HoldingRegister(patterns=(
            pattern(fix="BAYAN"),
            pattern(fix="ALSEM"),
        ))
        assert [p.fix for p in held.on_route(["ALSEM", "BAYAN"])] == [
            "ALSEM", "BAYAN"
        ]

    def test_a_fix_with_no_pattern_contributes_nothing(self):
        held = HoldingRegister(patterns=(pattern(),))
        assert held.on_route(["ZZZZZ"]) == ()

    def test_an_empty_query_returns_nothing_rather_than_everything(self):
        held = HoldingRegister(patterns=(pattern(region="AAAA"),))
        assert held.at_fix("") == ()
        assert held.in_region("  ") == ()
        assert held.at("") == ()


# --------------------------------------------------------------------------
# Reading a holding manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "holds.txt"
    path.write_text("a holding table, standing in for one somebody read\n",
                    encoding="utf-8")
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "holds.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP ENR 3.6",
            "document_path": "holds.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "patterns": [
            {
                "fix": "ALSEM", "inbound_track_deg": 360, "turn": "right",
                "minimum_ft": 8000, "maximum_ft": 24000,
                "outbound_time_min": 1.0, "speed_limit_kt": 220,
                "locator": "ENR 3.6 row 1",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_pattern_cited(self, tmp_path, document):
        held = load_holding(write(tmp_path, manifest()))
        found = held.at_fix("ALSEM")[0]
        assert found.source.locator == "ENR 3.6 row 1"
        assert found.inbound_track_deg == 360.0
        assert len(found.source.content_hash) == 64

    def test_a_loaded_pattern_screens(self, tmp_path, document):
        held = load_holding(write(tmp_path, manifest()))
        found = screen_holding(held.at_fix("ALSEM")[0], level_ft=30000)
        assert found

    def test_the_turn_direction_is_required(self, tmp_path, document):
        """It decides which side is protected and which entry applies."""
        payload = manifest()
        del payload["patterns"][0]["turn"]
        with pytest.raises(ManifestError, match="turn must be"):
            load_holding(write(tmp_path, payload))

    def test_the_inbound_track_is_required(self, tmp_path, document):
        payload = manifest()
        del payload["patterns"][0]["inbound_track_deg"]
        with pytest.raises(ManifestError, match="inbound_track_deg is required"):
            load_holding(write(tmp_path, payload))

    def test_a_pattern_needs_a_locator(self, tmp_path, document):
        payload = manifest()
        del payload["patterns"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_holding(write(tmp_path, payload))

    def test_an_unreadable_figure_is_refused_not_guessed(self, tmp_path, document):
        payload = manifest()
        payload["patterns"][0]["maximum_ft"] = "as directed"
        with pytest.raises(ManifestError, match="not a number"):
            load_holding(write(tmp_path, payload))

    def test_the_documents_region_applies_where_none_is_named(self, tmp_path, document):
        held = load_holding(write(tmp_path, manifest()))
        assert held.at_fix("ALSEM")[0].region == "AAAA"

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(holding_template())
        assert blank["patterns"][0]["turn"] == "right"
        assert blank["patterns"][0]["inbound_track_deg"] is None
