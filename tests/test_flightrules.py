"""ENR 1.3 — the rule a blank direction column defers to.

Most segments of ENR 3 print nothing in the direction column, because nothing
about them departs from the State's general rule. Until this module existed
that blank was read as "both", which cleared every level on the commonest case
in the whole route structure. So the assertions here are about what is *not*
answered: an unread ENR 1.3, an unheld track, a level above the band where
parity is the rule, and a scheme that is a table rather than a parity.

The other half is the departures. The Annex draws the sectors 000°–180°; a
State may draw them elsewhere, may reverse which set each takes, and may not
say whether the track is magnetic or true. Each of those is held as published,
because each of them is the reason the section is printed at all.

Nothing here is a claim about a real State's rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aeropub.ats import CruisingLevels
from aeropub.flightrules import (
    ANNEX_2_PARITY_CEILING_FT,
    CruisingLevelScheme,
    FlightRulesRegister,
    LevelScheme,
    TrackBasis,
    flight_rules_template,
    load_flight_rules,
    view_flight_rules,
)
from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 1.3 para 2",
        retrieved_at=NOW,
        content_hash="f" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def scheme(region: str = "AAAA", **overrides) -> CruisingLevelScheme:
    fields = dict(
        region=region,
        source=ref(),
        scheme=LevelScheme.SEMICIRCULAR,
        basis=TrackBasis.MAGNETIC,
    )
    fields.update(overrides)
    return CruisingLevelScheme(**fields)


def register(*schemes, covers=()) -> FlightRulesRegister:
    held = schemes or (scheme(),)
    return FlightRulesRegister(
        schemes=held,
        covers=frozenset(covers) | {s.region for s in held},
    )


# --------------------------------------------------------------------------
# The sectors
# --------------------------------------------------------------------------


class TestSectors:
    def test_the_annex_sectors_split_at_north_and_south(self):
        held = scheme()
        assert held.in_sector(0.0)
        assert held.in_sector(179.0)
        assert not held.in_sector(180.0)
        assert not held.in_sector(359.0)

    def test_the_easterly_sector_takes_odd_levels_by_default(self):
        assert scheme().parity_for(90.0) is CruisingLevels.ODD
        assert scheme().parity_for(270.0) is CruisingLevels.EVEN

    def test_a_state_may_draw_the_sectors_elsewhere(self):
        """The departure is the whole reason ENR 1.3 prints them."""
        held = scheme(sector_from_deg=90.0, sector_to_deg=270.0)
        assert held.parity_for(180.0) is CruisingLevels.ODD
        assert held.parity_for(0.0) is CruisingLevels.EVEN

    def test_a_sector_may_wrap_through_north(self):
        held = scheme(sector_from_deg=270.0, sector_to_deg=90.0)
        assert held.in_sector(0.0)
        assert held.in_sector(350.0)
        assert not held.in_sector(180.0)

    def test_a_state_may_reverse_which_set_the_sector_takes(self):
        held = scheme(sector_parity=CruisingLevels.EVEN)
        assert held.parity_for(90.0) is CruisingLevels.EVEN
        assert held.parity_for(270.0) is CruisingLevels.ODD

    def test_a_sector_taking_both_or_neither_is_not_a_semicircular_rule(self):
        with pytest.raises(ValueError, match="odd or even"):
            scheme(sector_parity=CruisingLevels.BOTH)

    def test_a_bearing_out_of_range_is_brought_into_range(self):
        assert scheme(sector_from_deg=360.0).sector_from_deg == 0.0
        assert scheme().parity_for(450.0) is CruisingLevels.ODD


# --------------------------------------------------------------------------
# What it refuses to answer
# --------------------------------------------------------------------------


class TestRefusals:
    def test_an_unread_section_permits_nothing_and_refuses_nothing(self):
        held = scheme(scheme=LevelScheme.UNREAD)
        assert held.permits(35000.0, 90.0) is None
        assert held.parity_for(90.0) is None

    def test_a_segment_with_no_track_cannot_be_placed_in_a_sector(self):
        assert scheme().permits(35000.0, None) is None

    def test_a_level_above_the_parity_band_is_not_answered(self):
        """Annex 2 runs on 1000 ft steps to FL410 and 4000 ft above it, and
        the upper part is not a parity: FL450 sits with FL350 while being an
        even number of thousands."""
        held = scheme()
        assert held.permits(41000.0, 90.0) is True
        assert held.permits(43000.0, 90.0) is None
        assert held.permits(45000.0, 90.0) is None

    def test_the_parity_ceiling_is_the_annex_default(self):
        assert scheme().parity_ceiling_ft == ANNEX_2_PARITY_CEILING_FT
        assert ANNEX_2_PARITY_CEILING_FT == 41000.0

    def test_a_state_publishing_no_ceiling_leaves_the_whole_band_open(self):
        assert scheme(parity_ceiling_ft=None).permits(35000.0, 90.0) is None

    def test_a_regional_table_is_not_evaluated(self):
        """The table is the answer, and reading it out of a paragraph is not
        something a parser should do."""
        held = scheme(scheme=LevelScheme.REGIONAL_TABLE)
        assert held.permits(35000.0, 90.0) is None

    def test_a_metric_scheme_is_not_converted(self):
        held = scheme(scheme=LevelScheme.METRIC)
        assert held.permits(35000.0, 90.0) is None

    def test_a_section_read_and_prescribing_nothing_is_not_a_pass(self):
        held = scheme(scheme=LevelScheme.NOT_PUBLISHED)
        assert held.permits(35000.0, 90.0) is None


class TestPermits:
    def test_an_easterly_track_takes_an_odd_level(self):
        assert scheme().permits(35000.0, 90.0) is True
        assert scheme().permits(36000.0, 90.0) is False

    def test_a_westerly_track_takes_an_even_level(self):
        assert scheme().permits(36000.0, 270.0) is True
        assert scheme().permits(35000.0, 270.0) is False

    def test_a_level_that_is_not_a_whole_hundred_is_refused(self):
        assert scheme().permits(35050.0, 90.0) is False


# --------------------------------------------------------------------------
# Magnetic or true
# --------------------------------------------------------------------------


class TestTrackBasis:
    def test_an_unstated_basis_is_reported_rather_than_picked(self):
        view = view_flight_rules(
            register(scheme(basis=TrackBasis.NOT_STATED)), regions=["AAAA"]
        )
        assert view.basis_not_stated == ("AAAA",)

    def test_a_stated_basis_raises_nothing(self):
        view = view_flight_rules(
            register(scheme(basis=TrackBasis.TRUE)), regions=["AAAA"]
        )
        assert view.basis_not_stated == ()

    def test_it_says_why_the_basis_matters(self):
        view = view_flight_rules(
            register(scheme(basis=TrackBasis.NOT_STATED)), regions=["AAAA"]
        )
        assert "magnetic variation" in view.render()

    def test_no_margin_is_invented_for_it(self):
        """The margin that would matter is the local variation, and nothing
        here holds that."""
        import aeropub.flightrules as module

        source = " ".join((module.__doc__ or "").split())
        assert "no margin is invented here" in source

    def test_an_unstated_basis_still_answers_the_parity(self):
        """Reporting the gap is not refusing the question."""
        held = scheme(basis=TrackBasis.NOT_STATED)
        assert held.permits(35000.0, 90.0) is True

    def test_the_finding_names_the_basis_it_assumed_nothing_about(self):
        view = view_flight_rules(
            register(scheme(basis=TrackBasis.NOT_STATED)),
            regions=["AAAA"],
            planned_ft=36000.0,
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", 90.0)],
        )
        assert "does not say magnetic or true" in view.findings[0].describe()


# --------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------


class TestView:
    def test_a_region_never_read_is_a_row_not_a_silence(self):
        view = view_flight_rules(FlightRulesRegister(), regions=["AAAA"])
        assert view.unread_regions == ("AAAA",)
        assert not view.is_conclusive
        assert "never read" in view.render()

    def test_read_and_prescribing_nothing_is_kept_apart_from_never_read(self):
        held = FlightRulesRegister(covers=frozenset({"AAAA"}))
        view = view_flight_rules(held, regions=["AAAA"])
        assert view.unread_regions == ()
        assert view.no_scheme == ("AAAA",)

    def test_a_level_against_the_direction_is_a_finding(self):
        view = view_flight_rules(
            register(),
            regions=["AAAA"],
            planned_ft=36000.0,
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", 90.0)],
        )
        assert len(view.findings) == 1
        assert view.findings[0].expected is CruisingLevels.ODD
        assert "UM688 ALSEM-MIDLE" in view.findings[0].describe()

    def test_a_level_with_the_direction_raises_nothing(self):
        view = view_flight_rules(
            register(),
            regions=["AAAA"],
            planned_ft=35000.0,
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", 90.0)],
        )
        assert view.findings == ()

    def test_a_segment_with_no_track_is_unscreened_not_clear(self):
        view = view_flight_rules(
            register(),
            regions=["AAAA"],
            planned_ft=35000.0,
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", None)],
        )
        assert view.findings == ()
        assert "published no track" in view.unscreened[0]
        assert not view.is_conclusive

    def test_a_segment_in_a_region_with_no_scheme_is_unscreened(self):
        view = view_flight_rules(
            register(),
            regions=["AAAA"],
            planned_ft=35000.0,
            tracks=[("L604 KUKLA-RASKI", "BBBB", 270.0)],
        )
        assert "no ENR 1.3 scheme held for BBBB" in view.unscreened[0]

    def test_a_level_above_the_parity_band_is_unscreened(self):
        view = view_flight_rules(
            register(),
            regions=["AAAA"],
            planned_ft=43000.0,
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", 90.0)],
        )
        assert view.findings == ()
        assert "above the band" in view.unscreened[0]

    def test_a_table_scheme_says_it_is_a_table(self):
        view = view_flight_rules(
            register(scheme(scheme=LevelScheme.REGIONAL_TABLE)),
            regions=["AAAA"],
            planned_ft=35000.0,
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", 90.0)],
        )
        assert "table rather than a parity" in view.unscreened[0]

    def test_no_planned_level_screens_nothing_and_claims_nothing(self):
        view = view_flight_rules(
            register(),
            regions=["AAAA"],
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", 90.0)],
        )
        assert view.findings == ()
        assert view.unscreened == ()
        assert view.is_conclusive

    def test_a_fully_read_screen_with_nothing_wrong_is_conclusive(self):
        view = view_flight_rules(
            register(),
            regions=["AAAA"],
            planned_ft=35000.0,
            tracks=[("UM688 ALSEM-MIDLE", "AAAA", 90.0)],
        )
        assert view.is_conclusive


# --------------------------------------------------------------------------
# What ENR 3's blank column now means
# --------------------------------------------------------------------------


class TestBlankDirectionColumn:
    def test_a_segment_publishing_nothing_permits_nothing_and_refuses_nothing(self):
        assert CruisingLevels.NOT_PUBLISHED.permits(35000.0) is None
        assert CruisingLevels.NOT_PUBLISHED.permits(36000.0) is None

    def test_both_still_means_the_state_published_both(self):
        assert CruisingLevels.BOTH.permits(35000.0) is True

    def test_none_still_refuses(self):
        assert CruisingLevels.NONE.permits(35000.0) is False

    def test_a_blank_column_is_read_as_not_published(self):
        from aeropub.ats import RouteSegment

        segment = RouteSegment(route="UM688", start="ALSEM", end="MIDLE", source=ref())
        assert segment.direction is CruisingLevels.NOT_PUBLISHED

    def test_it_sends_the_reader_to_enr_1_3(self):
        from aeropub.ats import RouteSegment

        segment = RouteSegment(route="UM688", start="ALSEM", end="MIDLE", source=ref())
        assert "ENR 1.3" in segment.describe()


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "TEST",
            "document": "AIP AA ENR 1.3",
            "retrieved_at": "2026-09-01T00:00:00Z",
            "content_hash": "a" * 64,
        },
        "region": "AAAA",
        "schemes": [
            {
                "scheme": "semicircular",
                "basis": "magnetic",
                "sector_from": 0,
                "sector_to": 180,
                "sector_parity": "odd",
                "parity_ceiling": "FL410",
                "minimum_rule": "1000 FT above the highest obstacle within 8 KM",
                "locator": "ENR 1.3 para 2.1",
            }
        ],
    }
    payload.update(overrides)
    return payload


def write(tmp_path, name: str, payload: dict):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoading:
    def test_a_scheme_is_read_with_its_citation(self, tmp_path):
        held = load_flight_rules(write(tmp_path, "enr13.json", manifest()))
        found = held.for_region("AAAA")
        assert found is not None
        assert found.source.locator == "ENR 1.3 para 2.1"
        assert found.basis is TrackBasis.MAGNETIC

    def test_a_flight_level_ceiling_is_read(self, tmp_path):
        held = load_flight_rules(write(tmp_path, "enr13.json", manifest()))
        assert held.for_region("AAAA").parity_ceiling_ft == 41000.0

    def test_a_row_with_no_locator_is_refused(self, tmp_path):
        payload = manifest()
        del payload["schemes"][0]["locator"]
        with pytest.raises(ManifestError, match="locator is required"):
            load_flight_rules(write(tmp_path, "enr13.json", payload))

    def test_an_unreadable_sector_bound_is_refused_not_defaulted(self, tmp_path):
        """Defaulting to the Annex would erase the departure the section
        exists to publish."""
        payload = manifest()
        payload["schemes"][0]["sector_from"] = "east"
        with pytest.raises(ManifestError, match="never rounded"):
            load_flight_rules(write(tmp_path, "enr13.json", payload))

    def test_an_unknown_scheme_is_refused(self, tmp_path):
        payload = manifest()
        payload["schemes"][0]["scheme"] = "quadrantal"
        with pytest.raises(ManifestError, match="scheme must be one of"):
            load_flight_rules(write(tmp_path, "enr13.json", payload))

    def test_a_region_read_with_no_schemes_is_covered_not_absent(self, tmp_path):
        payload = manifest(schemes=[])
        held = load_flight_rules(write(tmp_path, "enr13.json", payload))
        assert held.is_read("AAAA")
        assert held.for_region("AAAA") is None

    def test_covers_records_a_region_read_and_empty(self, tmp_path):
        payload = manifest(schemes=[], covers=["BBBB"])
        held = load_flight_rules(write(tmp_path, "enr13.json", payload))
        assert held.is_read("BBBB")
        assert not held.is_read("CCCC")

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(flight_rules_template())
        assert blank["schemes"][0]["scheme"] == "semicircular"
        assert blank["schemes"][0]["basis"] == "not_stated"

    def test_the_template_does_not_claim_a_basis(self):
        """Which of magnetic or true a State uses is the departure that flips
        every level on a north-south route."""
        blank = json.loads(flight_rules_template())
        assert blank["schemes"][0]["basis"] == "not_stated"
