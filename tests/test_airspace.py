"""ENR 2 — the airspace you are inside, and the boundary that is the finding.

Two things are tested hardest here.

**The class transition is level-aware.** An FIR topping at FL195 says nothing
about a flight at FL350 — the flight is in the upper region above it. Reporting
the FIR's class there would be a confident answer about the wrong volume, and
it is the kind of mistake that reads perfectly until somebody acts on it.

**Nothing here is a containment verdict.** The platform holds no geometry. A
volume that altitude does not rule out is on the list; that is not the same as
a track entering it, and the document has to say so rather than let a reader
assume the stronger claim.

Every designator below is a fixture. None of it is a claim about real airspace.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.airspace import (
    AIRSPACE,
    UNLIMITED,
    Airspace,
    AirspaceClass,
    AirspaceStructure,
    AirspaceType,
    CarriageRequirement,
    ClassTransition,
    airspace_template,
    load_airspace,
    read_limit,
    view_airspace,
)
from aeropub.entities import named
from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 2.1",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def volume(designator: str, kind: AirspaceType, **overrides) -> Airspace:
    fields = dict(designator=designator, kind=kind, source=ref())
    fields.update(overrides)
    return Airspace(**fields)


#: An upper region in class A over a lower region in class G — the transition
#: that changes what the crew is responsible for.
ALPHA = volume(
    "AAAA", AirspaceType.FIR, airspace_class=AirspaceClass.A,
    lower_ft=24500, upper_ft=66000, unit="Alpha Control", frequency_mhz=127.5,
    requirements=(CarriageRequirement.RVSM, CarriageRequirement.CPDLC),
)
BRAVO = volume(
    "BBBB", AirspaceType.FIR, airspace_class=AirspaceClass.G,
    lower_ft=0, upper_ft=19500, unit="Bravo Information",
)
BRAVO_UPPER = volume(
    "BBBB", AirspaceType.UIR, airspace_class=AirspaceClass.C,
    region="BBBB", lower_ft=19500, upper_ft=66000, unit="Bravo Control",
)
BRAVO_TMA = volume(
    "BTMA", AirspaceType.TMA, airspace_class=AirspaceClass.C, region="BBBB",
    lower_ft=1500, upper_ft=9500, unit="Bravo Approach",
)


def structure(*volumes: Airspace) -> AirspaceStructure:
    return AirspaceStructure(volumes=volumes or (ALPHA, BRAVO, BRAVO_TMA))


# --------------------------------------------------------------------------
# What a class actually answers
# --------------------------------------------------------------------------


class TestAirspaceClass:
    def test_the_letters_answer_three_separate_questions(self):
        """Not a severity scale. A planner needs the answers, not the letter."""
        assert AirspaceClass.A.ifr_clearance_required
        assert AirspaceClass.A.ifr_separated_from_ifr
        assert not AirspaceClass.A.vfr_permitted

    def test_only_class_a_forbids_vfr(self):
        for letter in "bcdefg":
            assert AirspaceClass(letter).vfr_permitted, letter

    def test_class_f_offers_separation_and_class_e_provides_it(self):
        """The distinction crews most often lose.

        It is the difference between a service and a promise, and it changes
        who is responsible for not hitting anything.
        """
        assert AirspaceClass.E.ifr_separated_from_ifr
        assert not AirspaceClass.F.ifr_separated_from_ifr
        assert not AirspaceClass.G.ifr_separated_from_ifr

    def test_unclassified_answers_nothing_rather_than_defaulting(self):
        """Guessing G understates the clearance requirement, A overstates it."""
        assert AirspaceClass.UNCLASSIFIED.ifr_clearance_required is None
        assert AirspaceClass.UNCLASSIFIED.vfr_permitted is None
        assert "not held" in AirspaceClass.UNCLASSIFIED.describe()


class TestVolume:
    def test_a_volume_is_keyed_free_standing(self):
        """Airspace belongs to no aerodrome."""
        assert ALPHA.key == named(AIRSPACE, "AAAA")

    def test_a_fir_is_its_own_region(self):
        assert ALPHA.belongs_to == "AAAA"
        assert BRAVO_TMA.belongs_to == "BBBB"

    def test_altitude_rules_a_volume_out_or_does_not(self):
        assert BRAVO_TMA.reaches(5000) is True
        assert BRAVO_TMA.reaches(35000) is False

    def test_a_volume_with_no_limits_can_never_be_ruled_out(self):
        """The one false negative that matters: out of the way, unread."""
        unbounded = volume("CCCC", AirspaceType.FIR)
        assert unbounded.reaches(35000) is None

    def test_an_inverted_pair_of_limits_is_refused(self):
        with pytest.raises(ValueError, match="above upper"):
            volume("DDDD", AirspaceType.FIR, lower_ft=20000, upper_ft=10000)

    def test_a_volume_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            Airspace(designator="AAAA", kind=AirspaceType.FIR, source=None)


class TestLimits:
    def test_the_forms_an_aip_prints(self):
        assert read_limit("SFC") == 0.0
        assert read_limit("GND") == 0.0
        assert read_limit("UNL") == UNLIMITED
        assert read_limit("FL195") == 19500.0
        assert read_limit("2500FT") == 2500.0
        assert read_limit(3000) == 3000.0

    def test_anything_else_is_refused_rather_than_guessed(self):
        """A guessed limit rules a volume out and nobody sees it happen."""
        with pytest.raises(ManifestError, match="could not be read"):
            read_limit("by NOTAM")

    def test_nothing_reads_as_nothing(self):
        assert read_limit(None) is None
        assert read_limit("") is None


# --------------------------------------------------------------------------
# The boundary is the finding
# --------------------------------------------------------------------------


class TestTransitions:
    def test_a_class_change_across_a_boundary_is_reported(self):
        view = view_airspace(structure(), regions=["AAAA", "BBBB"], planned_ft=30000)
        # AAAA reaches 30000; BBBB tops at 19500, so it is the upper region
        # that applies and nobody has read one.
        assert len(view.transitions) == 1

    def test_the_class_is_taken_from_the_volume_that_reaches_the_level(self):
        """An FIR topping at FL195 says nothing about a flight at FL350."""
        view = view_airspace(
            structure(ALPHA, BRAVO, BRAVO_UPPER), regions=["AAAA", "BBBB"],
            planned_ft=35000,
        )
        boundary = view.transitions[0]
        assert boundary.is_known
        assert boundary.to_class is AirspaceClass.C
        assert boundary.to_unit == "Bravo Control"

    def test_a_level_above_everything_held_says_which_gap_it_is(self):
        """Two different gaps hide behind "class not held"."""
        view = view_airspace(
            structure(ALPHA, BRAVO), regions=["AAAA", "BBBB"], planned_ft=35000
        )
        boundary = view.transitions[0]
        assert not boundary.is_known
        assert "above what is held" in boundary.describe()

    def test_a_region_nobody_read_says_so_differently(self):
        view = view_airspace(
            structure(), regions=["AAAA", "ZZZZ"], planned_ft=30000
        )
        assert "not read" in view.transitions[0].describe()

    def test_losing_ifr_separation_is_called_out(self):
        """The transition a table of letters hides."""
        low = volume(
            "BBBB", AirspaceType.FIR, airspace_class=AirspaceClass.G,
            lower_ft=0, upper_ft=66000, unit="Bravo Information",
        )
        view = view_airspace(
            structure(ALPHA, low), regions=["AAAA", "BBBB"], planned_ft=30000
        )
        boundary = view.transitions[0]
        assert boundary.loses_separation
        assert "IFR separation no longer provided" in boundary.describe()

    def test_gaining_separation_is_not_a_finding(self):
        transition = ClassTransition(
            leaving="X", entering="Y",
            from_class=AirspaceClass.G, to_class=AirspaceClass.A,
        )
        assert not transition.loses_separation

    def test_one_region_produces_no_boundaries(self):
        assert view_airspace(structure(), regions=["AAAA"]).transitions == ()


class TestView:
    def test_altitude_eliminates_and_says_it_eliminated(self):
        view = view_airspace(structure(), regions=["BBBB"], planned_ft=35000)
        assert BRAVO_TMA in view.eliminated
        assert BRAVO_TMA not in view.volumes

    def test_a_volume_with_no_limits_is_kept_apart_from_the_candidates(self):
        """Our gap, not the airspace's."""
        unbounded = volume("CCCC", AirspaceType.FIR)
        view = view_airspace(structure(unbounded), regions=["CCCC"], planned_ft=35000)
        assert view.unbounded == (unbounded,)
        assert view.volumes == ()
        assert not view.is_conclusive

    def test_an_unread_region_is_named_not_omitted(self):
        view = view_airspace(structure(), regions=["ZZZZ"], planned_ft=35000)
        assert view.unread_regions == ("ZZZZ",)
        assert "nothing read is the same shape as nothing found" in view.render()

    def test_carriage_requirements_are_collected_across_the_route(self):
        view = view_airspace(structure(), regions=["AAAA"], planned_ft=35000)
        assert set(view.requirements) == {
            CarriageRequirement.RVSM, CarriageRequirement.CPDLC
        }
        assert "whatever the flight plan says" in view.render()

    def test_units_come_out_in_order_of_crossing(self):
        view = view_airspace(
            structure(ALPHA, BRAVO_UPPER), regions=["AAAA", "BBBB"], planned_ft=35000
        )
        assert view.units == ("Alpha Control", "Bravo Control")

    def test_the_document_refuses_the_containment_claim(self):
        text = view_airspace(structure(), regions=["AAAA"], planned_ft=35000).render()
        assert "holds no" in text and "geometry" in text

    def test_an_empty_view_is_never_conclusive_on_its_own(self):
        """An empty list is the same shape as a clear one."""
        assert not view_airspace(structure(), regions=[]).is_conclusive


# --------------------------------------------------------------------------
# Reading an ENR 2 manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr2.txt"
    path.write_text("an ENR 2 table, standing in for one somebody read\n",
                    encoding="utf-8")
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "enr2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP ENR 2.1",
            "document_path": "enr2.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "volumes": [
            {
                "designator": "AAAA", "kind": "fir", "class": "a",
                "lower": "FL245", "upper": "FL660", "unit": "Alpha Control",
                "frequency_mhz": 127.5, "requirements": ["rvsm", "cpdlc"],
                "locator": "ENR 2.1 row 1",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_structure_loads_with_every_volume_cited(self, tmp_path, document):
        held = load_airspace(write(tmp_path, manifest()))
        found = held.volume("AAAA")
        assert found.source.locator == "ENR 2.1 row 1"
        assert len(found.source.content_hash) == 64
        assert found.lower_ft == 24500.0

    def test_carriage_requirements_read_in_the_forms_people_write(self, tmp_path, document):
        payload = manifest()
        payload["volumes"][0]["requirements"] = ["RVSM", "ads-b"]
        held = load_airspace(write(tmp_path, payload))
        assert held.volume("AAAA").requirements == (
            CarriageRequirement.RVSM, CarriageRequirement.ADS_B
        )

    def test_a_class_left_out_comes_back_unclassified(self, tmp_path, document):
        payload = manifest()
        del payload["volumes"][0]["class"]
        held = load_airspace(write(tmp_path, payload))
        assert held.volume("AAAA").airspace_class is AirspaceClass.UNCLASSIFIED

    def test_an_unknown_class_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["volumes"][0]["class"] = "h"
        with pytest.raises(ManifestError, match="class must be"):
            load_airspace(write(tmp_path, payload))

    def test_an_unknown_type_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["volumes"][0]["kind"] = "sector"
        with pytest.raises(ManifestError, match="kind must be"):
            load_airspace(write(tmp_path, payload))

    def test_a_volume_needs_a_locator(self, tmp_path, document):
        payload = manifest()
        del payload["volumes"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_airspace(write(tmp_path, payload))

    def test_an_unreadable_limit_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["volumes"][0]["upper"] = "as notified"
        with pytest.raises(ManifestError, match="could not be read"):
            load_airspace(write(tmp_path, payload))

    def test_the_documents_region_applies_to_volumes_that_name_none(
        self, tmp_path, document
    ):
        payload = manifest()
        payload["volumes"].append({
            "designator": "ATMA", "kind": "tma", "class": "c",
            "lower": "1500", "upper": "FL095", "locator": "ENR 2.1 row 2",
        })
        held = load_airspace(write(tmp_path, payload))
        assert held.volume("ATMA").region == "AAAA"

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(airspace_template())
        assert blank["volumes"][0]["kind"] == "fir"
        assert blank["volumes"][0]["class"] == ""
