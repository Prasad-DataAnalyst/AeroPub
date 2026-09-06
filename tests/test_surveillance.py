"""ENR 1.6 — the class says you are separated; this says how.

ENR 2 publishes a class, and a class is a promise about service. It never says
by what means. A region publishing Class A to FL660 and surveillance from
FL200 is Class A at FL180 too, and the separation there is procedural: longer
spacing, position reports, a controller who cannot see you. Neither section
says so alone.

The assertions are about that cross-check and about the three ways it could
mislead.

**A blank coverage column is not coverage to the ground.** Read-and-blank and
never-read are both reported, and neither is "covered".

**Below the floor is not "no service".** It is separation provided
procedurally, and a crew reading it as no service would be wrong the other way.

**A primary return is an echo.** It has no identity and no level, so "radar"
is two different things and the kinds keep them apart.

Every region and level below is a fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef
from aeropub.surveillance import (
    ServiceLevel,
    Surveillance,
    SurveillanceKind,
    SurveillanceRegister,
    load_surveillance,
    surveillance_template,
    view_surveillance,
)

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 1.6",
        retrieved_at=NOW,
        content_hash="f" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def service(region: str, **overrides) -> Surveillance:
    fields = dict(region=region, source=ref())
    fields.update(overrides)
    return Surveillance(**fields)


COVERED = service(
    "AAAA",
    kinds=(SurveillanceKind.SSR, SurveillanceKind.MODE_S),
    service=ServiceLevel.RADAR_CONTROL,
    floor_ft=20000.0,
    ceiling_ft=66000.0,
    carriage=("Mode S EHS",),
    unit="Alpha Control",
)
OCEANIC = service(
    "BBBB",
    kinds=(SurveillanceKind.ADS_C,),
    service=ServiceLevel.PROCEDURAL,
    floor_ft=0.0,
)
BLANK = service(
    "CCCC",
    kinds=(SurveillanceKind.SSR,),
    service=ServiceLevel.RADAR_CONTROL,
)


def register(*services, covers: tuple[str, ...] = ()) -> SurveillanceRegister:
    held = services or (COVERED, OCEANIC, BLANK)
    return SurveillanceRegister(services=held, covers=frozenset(covers))


# --------------------------------------------------------------------------
# What the kinds mean
# --------------------------------------------------------------------------


class TestKinds:
    def test_a_primary_return_does_not_identify_anything(self):
        """An echo with a position and nothing else."""
        assert not SurveillanceKind.PSR.identifies_aircraft
        assert not SurveillanceKind.PSR.is_cooperative

    def test_secondary_radar_does(self):
        assert SurveillanceKind.SSR.identifies_aircraft
        assert SurveillanceKind.MODE_S.identifies_aircraft
        assert SurveillanceKind.ADS_B.identifies_aircraft

    def test_ads_c_is_not_a_continuous_picture(self):
        """A controller working ADS-C has a sequence of positions, not a
        display."""
        assert SurveillanceKind.ADS_C.identifies_aircraft
        assert not SurveillanceKind.ADS_C.is_continuous
        assert SurveillanceKind.SSR.is_continuous

    def test_published_as_none_is_an_answer(self):
        assert not SurveillanceKind.NONE.is_continuous
        assert not SurveillanceKind.NONE.identifies_aircraft

    def test_a_psr_only_region_cannot_identify(self):
        assert not service("Z", kinds=(SurveillanceKind.PSR,)).identifies

    def test_the_label_is_the_form_a_reader_knows(self):
        assert SurveillanceKind.MODE_S.label == "MODE-S"
        assert SurveillanceKind.ADS_B.label == "ADS-B"


class TestService:
    def test_procedural_is_separation_provided_differently(self):
        """A reader who took it for no service would be wrong."""
        assert ServiceLevel.PROCEDURAL.separates is True
        assert not ServiceLevel.PROCEDURAL.needs_surveillance

    def test_radar_advisory_is_not_separation(self):
        assert ServiceLevel.RADAR_ADVISORY.separates is False
        assert ServiceLevel.RADAR_ADVISORY.needs_surveillance

    def test_an_unstated_service_answers_neither(self):
        assert ServiceLevel.NOT_STATED.separates is None


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


class TestCoverage:
    def test_a_level_inside_the_published_coverage_is_covered(self):
        assert COVERED.covers(35000.0) is True

    def test_a_level_below_the_floor_is_not(self):
        assert COVERED.covers(18000.0) is False

    def test_a_level_above_the_ceiling_is_not(self):
        assert COVERED.covers(70000.0) is False

    def test_no_published_coverage_answers_neither(self):
        """A blank column is not coverage throughout — it is a column nobody
        filled in."""
        assert BLANK.covers(35000.0) is None
        assert not BLANK.coverage_known

    def test_a_floor_above_a_ceiling_is_refused(self):
        with pytest.raises(ValueError, match="never covers anything"):
            service("Z", floor_ft=40000.0, ceiling_ft=10000.0)

    def test_an_open_ended_band_says_which_end_is_open(self):
        assert "no ceiling published" in OCEANIC.describe()
        assert "up to 20000 ft, no floor published" in service(
            "Z", ceiling_ft=20000.0
        ).describe()

    def test_a_service_with_no_region_is_refused(self):
        with pytest.raises(ValueError, match="region"):
            Surveillance(region="", source=ref())


# --------------------------------------------------------------------------
# The cross-check
# --------------------------------------------------------------------------


class TestTheFinding:
    def test_a_plan_below_the_floor_is_a_finding(self):
        found = view_surveillance(
            register(), regions=["AAAA"], planned_ft=18000.0
        )
        assert len(found.gaps) == 1
        assert found.gaps[0].short_by_ft == 2000.0

    def test_the_finding_says_the_separation_is_procedural_not_absent(self):
        found = view_surveillance(
            register(), regions=["AAAA"], planned_ft=18000.0
        )
        text = found.gaps[0].describe()
        assert "procedurally rather than on a display" in text
        assert "no service" not in text

    def test_the_class_from_enr_2_is_what_makes_it_land(self):
        """Class A at a level with no surveillance is still Class A."""
        found = view_surveillance(
            register(),
            regions=["AAAA"],
            planned_ft=18000.0,
            classes={"AAAA": "a"},
        )
        text = found.gaps[0].describe()
        assert "still Class A" in text
        assert "separation is still provided" in text

    def test_the_class_is_supplied_not_looked_up(self):
        """This module is about surveillance; the class is what makes the
        finding land, not what produces it."""
        found = view_surveillance(
            register(), regions=["AAAA"], planned_ft=18000.0
        )
        assert found.gaps[0].airspace_class == ""
        assert "still Class" not in found.gaps[0].describe()

    def test_a_plan_inside_the_coverage_is_not_a_finding(self):
        assert view_surveillance(
            register(), regions=["AAAA"], planned_ft=35000.0
        ).gaps == ()

    def test_no_level_screens_no_coverage(self):
        """Reporting every region as covered because nobody said what level
        would be the worst possible default."""
        found = view_surveillance(register(), regions=["AAAA"])
        assert found.gaps == ()
        assert "no level given" in found.render()

    def test_a_region_with_no_coverage_figure_is_reported_separately(self):
        found = view_surveillance(
            register(), regions=["CCCC"], planned_ft=35000.0
        )
        assert found.gaps == ()
        assert found.unpublished_coverage == ("CCCC",)
        assert not found.is_conclusive
        assert "not coverage throughout" in found.render()

    def test_an_unread_region_is_named(self):
        found = view_surveillance(
            register(), regions=["ZZZZ"], planned_ft=35000.0
        )
        assert found.unread_regions == ("ZZZZ",)
        assert "not something the held documents answer" in found.render()

    def test_a_region_declared_read_with_no_rows_is_not_unread(self):
        held = register(COVERED, covers=("DDDD",))
        found = view_surveillance(held, regions=["DDDD"], planned_ft=35000.0)
        assert found.unread_regions == ()

    def test_the_mandated_carriage_is_collected_with_who_mandates_it(self):
        found = view_surveillance(
            register(), regions=["AAAA", "BBBB"], planned_ft=35000.0
        )
        assert found.carriage == (("AAAA", "Mode S EHS"),)
        assert "AAAA: Mode S EHS" in found.render()

    def test_an_empty_region_matches_nothing(self):
        assert register().in_region("") == ()


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr16.txt"
    path.write_text(
        "an ENR 1.6 paragraph, standing in for one somebody read\n",
        encoding="utf-8",
    )
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "enr16.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP AA ENR 1.6",
            "document_path": "enr16.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "covers": ["BBBB"],
        "services": [
            {
                "kinds": ["ssr", "mode s"],
                "service": "radar control",
                "floor": "FL200",
                "ceiling": "FL660",
                "carriage": ["Mode S EHS"],
                "unit": "Alpha Control",
                "locator": "ENR 1.6 para 2",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_statement_cited(self, tmp_path, document):
        held = load_surveillance(write(tmp_path, manifest()))
        found = held.in_region("AAAA")[0]
        assert found.source.locator == "ENR 1.6 para 2"
        assert found.kinds == (SurveillanceKind.SSR, SurveillanceKind.MODE_S)

    def test_limits_read_as_an_aip_prints_them(self, tmp_path, document):
        held = load_surveillance(write(tmp_path, manifest()))
        found = held.in_region("AAAA")[0]
        assert found.floor_ft == 20000.0
        assert found.ceiling_ft == 66000.0

    def test_a_service_written_with_spaces_reads(self, tmp_path, document):
        held = load_surveillance(write(tmp_path, manifest()))
        assert held.in_region("AAAA")[0].service is ServiceLevel.RADAR_CONTROL

    def test_the_declared_regions_are_read_even_with_no_rows(self, tmp_path, document):
        held = load_surveillance(write(tmp_path, manifest()))
        assert held.is_read("BBBB")
        assert held.in_region("BBBB") == ()

    def test_a_row_without_a_locator_is_refused(self, tmp_path, document):
        payload = manifest()
        del payload["services"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_surveillance(write(tmp_path, payload))

    def test_an_unknown_kind_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["services"][0]["kinds"] = ["magic"]
        with pytest.raises(ManifestError, match="kinds"):
            load_surveillance(write(tmp_path, payload))

    def test_a_floor_that_cannot_be_read_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["services"][0]["floor"] = "see remarks"
        with pytest.raises(ManifestError, match="left unread rather than guessed"):
            load_surveillance(write(tmp_path, payload))

    def test_a_missing_floor_is_no_coverage_published(self, tmp_path, document):
        payload = manifest()
        del payload["services"][0]["floor"]
        del payload["services"][0]["ceiling"]
        held = load_surveillance(write(tmp_path, payload))
        assert not held.in_region("AAAA")[0].coverage_known

    def test_covers_must_be_a_list(self, tmp_path, document):
        with pytest.raises(ManifestError, match="covers"):
            load_surveillance(write(tmp_path, manifest(covers="BBBB")))

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(surveillance_template())
        assert blank["covers"] == []
        assert blank["services"][0]["floor"] == ""
