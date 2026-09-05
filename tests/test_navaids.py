"""ENR 4 — the small row most of an instrument procedure hangs from.

Three refusals carry this module.

**An aid not held is not an aid that is fine.** A route naming something the
register has never seen comes back as a held-nothing entry rather than being
dropped, because a screen that dropped it would get shorter as coverage got
worse.

**A NOTAM overrides a published status.** An aid published operational with a
NOTAM in force against it is not usable — it is unknown, and the planner has
to read the NOTAM. Returning the published value there would be the AIP
answering a question the NOTAM had already reopened.

**Substitution is not a lookup.** What else is published near a fix is a list.
Whether one aid may stand in for another in a particular procedure is an
operational approval this platform does not hold, and a function that appeared
to answer it would be read as answering it.

Every identifier below is a fixture. None of it is a claim about a real aid.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aeropub.entities import named
from aeropub.manifest import ManifestError
from aeropub.navaids import (
    NAVAID,
    Navaid,
    NavaidKind,
    NavaidRegister,
    NavaidStatus,
    alternatives_to,
    load_navaids,
    navaid_template,
    screen_navaids,
)
from aeropub.notam_register import (
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 4.1",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def navaid(ident: str, kind: NavaidKind, **overrides) -> Navaid:
    fields = dict(ident=ident, kind=kind, source=ref(), region="AAAA")
    fields.update(overrides)
    return Navaid(**fields)


ALP = navaid(
    "ALP", NavaidKind.VOR_DME, frequency_mhz=113.9, coverage_nm=200,
    coverage_ft=50000, status=NavaidStatus.OPERATIONAL, serves=("ALSEM", "UM688"),
)
BRV = navaid(
    "BRV", NavaidKind.NDB, frequency_mhz=0.375, coverage_nm=50,
    status=NavaidStatus.ON_TEST, serves=("ALSEM",),
)
DME1 = navaid("DME1", NavaidKind.DME, channel="86X", coverage_nm=120, serves=("ALSEM",))


def register(*navaids: Navaid) -> NavaidRegister:
    return NavaidRegister(navaids=navaids or (ALP, BRV, DME1))


def notam(identifier: str, ident: str) -> RegisteredNotam:
    return RegisteredNotam(
        identifier=identifier,
        subjects=(Subject(kind=SubjectKind.NAVAID, entity=named(NAVAID, ident)),),
        effective_start=NOW - timedelta(days=1),
        effective_end=NOW + timedelta(days=1),
        source=ref(locator=identifier),
        text="test fixture",
    )


# --------------------------------------------------------------------------
# What an aid provides
# --------------------------------------------------------------------------


class TestKinds:
    def test_a_dme_gives_distance_and_not_bearing(self):
        assert NavaidKind.DME.gives_distance
        assert not NavaidKind.DME.gives_bearing

    def test_a_vor_gives_bearing_and_not_distance(self):
        assert NavaidKind.VOR.gives_bearing
        assert not NavaidKind.VOR.gives_distance

    def test_a_vor_dme_gives_both(self):
        assert NavaidKind.VOR_DME.gives_bearing
        assert NavaidKind.VOR_DME.gives_distance

    def test_approach_aids_are_separated_from_route_aids(self):
        """An outage on one is an approach problem, not a route problem."""
        assert NavaidKind.ILS.is_approach_aid
        assert not NavaidKind.VOR.is_approach_aid


class TestStatus:
    def test_on_test_is_not_usable_even_though_it_is_radiating(self):
        """The dangerous one: on the air, identifying, and not to be used."""
        assert NavaidStatus.ON_TEST.usable is False
        assert BRV.is_usable is False

    def test_unknown_is_not_assumed_operational(self):
        """An aid assumed serviceable is one a route gets planned on."""
        assert NavaidStatus.UNKNOWN.usable is None
        assert navaid("ZZZ", NavaidKind.VOR).is_usable is None

    def test_operational_is_usable(self):
        assert ALP.is_usable is True


class TestCoverage:
    def test_published_coverage_answers_reach(self):
        assert ALP.covers(150) is True
        assert ALP.covers(250) is False

    def test_a_level_above_the_designated_coverage_is_out_of_reach(self):
        assert ALP.covers(150, 60000) is False
        assert ALP.covers(150, 40000) is True

    def test_no_published_coverage_never_reads_as_unlimited(self):
        """A route depending on an aid whose range nobody published is
        depending on a guess."""
        assert navaid("ZZZ", NavaidKind.VOR).covers(10) is None

    def test_an_aid_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            Navaid(ident="ALP", kind=NavaidKind.VOR, source=None)


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


class TestScreen:
    def test_each_named_aid_comes_back_with_what_is_known(self):
        found = screen_navaids(register(), ["ALP", "BRV"])
        assert [u.ident for u in found] == ["ALP", "BRV"]
        assert found[0].is_usable is True
        assert found[1].is_usable is False

    def test_an_aid_not_held_is_never_dropped(self):
        """A screen that dropped it would get shorter as coverage got worse."""
        found = screen_navaids(register(), ["ZZZ"])
        assert len(found) == 1
        assert not found[0].is_held
        assert found[0].is_usable is None
        assert "not in the held ENR 4" in found[0].describe()

    def test_a_notam_in_force_overrides_a_published_status(self):
        """The AIP must not answer a question the NOTAM has reopened."""
        notams = NotamRegister([notam("A0007/26", "ALP")])
        found = screen_navaids(register(), ["ALP"], notams=notams, at=NOW)[0]
        assert found.navaid.is_usable is True
        assert found.is_usable is None
        assert "1 NOTAM in force" in found.describe()

    def test_a_notam_against_an_unheld_aid_still_reaches_the_screen(self):
        notams = NotamRegister([notam("A0008/26", "ZZZ")])
        found = screen_navaids(register(), ["ZZZ"], notams=notams, at=NOW)[0]
        assert len(found.notams) == 1

    def test_what_uses_an_aid_travels_with_it(self):
        """Turns an outage into a list of consequences, not a single row."""
        found = screen_navaids(
            register(), ["ALP"], used_by={"ALP": ["UM688", "ILS 34L"]}
        )[0]
        assert found.used_by == ("UM688", "ILS 34L")
        assert "UM688" in found.describe()

    def test_blank_identifiers_are_skipped_not_reported(self):
        assert screen_navaids(register(), ["", "  "]) == ()


class TestLookup:
    def test_an_identifier_is_not_globally_unique(self):
        """Two States may both publish a KIA, and both are held."""
        held = NavaidRegister(navaids=(
            navaid("KIA", NavaidKind.VOR, region="AAAA"),
            navaid("KIA", NavaidKind.NDB, region="BBBB"),
        ))
        assert len(held.all_named("KIA")) == 2
        assert held.navaid("KIA") is not None

    def test_aids_can_be_found_by_region_and_by_aerodrome(self):
        """An approach aid sits in a region too — the two are not exclusive.

        Only the aerodrome field distinguishes an aid that serves a runway
        from one that serves the route, and both are in somebody's FIR.
        """
        approach = navaid("IXX", NavaidKind.ILS, aerodrome="XXXX")
        held = register(ALP, approach)
        assert set(held.in_region("AAAA")) == {ALP, approach}
        assert held.at("XXXX") == (approach,)

    def test_an_empty_query_returns_nothing_rather_than_everything(self):
        """The empty string is what an aid carries when it serves no aerodrome.

        Matching on it would answer "which aids are at nowhere" with a list of
        every en-route aid held — a confident answer to a question nobody
        asked.
        """
        held = register()
        assert held.at("") == ()
        assert held.in_region("  ") == ()
        assert held.serving("") == ()

    def test_aids_can_be_found_by_what_they_serve(self):
        assert {n.ident for n in register().serving("ALSEM")} == {"ALP", "BRV", "DME1"}


class TestAlternatives:
    def test_it_lists_what_else_is_published_near_the_same_fix(self):
        assert {n.ident for n in alternatives_to(register(), "ALP")} == {"BRV", "DME1"}

    def test_it_can_be_narrowed_to_aids_that_give_bearing(self):
        """A DME cannot replace a VOR, and a mixed list has to be filtered
        by the reader anyway."""
        assert [n.ident for n in alternatives_to(register(), "ALP", bearing=True)] == [
            "BRV"
        ]

    def test_an_unheld_aid_has_no_alternatives_to_list(self):
        assert alternatives_to(register(), "ZZZ") == ()

    def test_it_does_not_filter_by_serviceability(self):
        """It lists what is published. Whether an aid on test may be used in a
        procedure is an operational approval this platform does not hold, and
        filtering here would look like answering it."""
        assert BRV in alternatives_to(register(), "ALP")


# --------------------------------------------------------------------------
# Reading an ENR 4 manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr4.txt"
    path.write_text("an ENR 4 table, standing in for one somebody read\n",
                    encoding="utf-8")
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "enr4.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP ENR 4.1",
            "document_path": "enr4.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "navaids": [
            {
                "ident": "ALP", "kind": "vor_dme", "frequency_mhz": 113.9,
                "coverage_nm": 200, "coverage_ft": 50000,
                "status": "operational", "serves": ["ALSEM", "UM688"],
                "locator": "ENR 4.1 row 2",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_aid_cited(self, tmp_path, document):
        held = load_navaids(write(tmp_path, manifest()))
        found = held.navaid("ALP")
        assert found.source.locator == "ENR 4.1 row 2"
        assert found.frequency_mhz == 113.9
        assert len(found.source.content_hash) == 64

    def test_what_it_serves_loads(self, tmp_path, document):
        held = load_navaids(write(tmp_path, manifest()))
        assert held.navaid("ALP").serves == ("ALSEM", "UM688")

    def test_the_documents_region_applies_where_none_is_named(self, tmp_path, document):
        held = load_navaids(write(tmp_path, manifest()))
        assert held.navaid("ALP").region == "AAAA"

    def test_an_unknown_kind_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["navaids"][0]["kind"] = "beacon"
        with pytest.raises(ManifestError, match="kind must be"):
            load_navaids(write(tmp_path, payload))

    def test_an_unknown_status_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["navaids"][0]["status"] = "probably fine"
        with pytest.raises(ManifestError, match="status must be"):
            load_navaids(write(tmp_path, payload))

    def test_an_unreadable_frequency_is_refused_not_rounded(self, tmp_path, document):
        """A rounded frequency is a frequency nobody can tune."""
        payload = manifest()
        payload["navaids"][0]["frequency_mhz"] = "113.9 / 113.95"
        with pytest.raises(ManifestError, match="not a number"):
            load_navaids(write(tmp_path, payload))

    def test_an_aid_needs_a_locator(self, tmp_path, document):
        payload = manifest()
        del payload["navaids"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_navaids(write(tmp_path, payload))

    def test_an_aerodrome_key_naming_an_object_on_one_is_refused(
        self, tmp_path, document
    ):
        payload = manifest()
        payload["navaids"][0]["aerodrome"] = "XXXX/RWY34L"
        with pytest.raises(ManifestError, match="names an object on an aerodrome"):
            load_navaids(write(tmp_path, payload))

    def test_a_status_left_out_comes_back_unknown(self, tmp_path, document):
        payload = manifest()
        del payload["navaids"][0]["status"]
        held = load_navaids(write(tmp_path, payload))
        assert held.navaid("ALP").status is NavaidStatus.UNKNOWN

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(navaid_template())
        assert blank["navaids"][0]["coverage_nm"] is None
        assert blank["navaids"][0]["serves"] == []


class TestPublishedCoordinates:
    """ENR 4.1 prints a coordinate column for every aid."""

    def test_an_aid_with_held_coordinates_has_a_position(self):
        held = navaid(
            "ALP", NavaidKind.VOR_DME, latitude=25.2731, longitude=51.6081
        )
        assert held.position is not None

    def test_an_aid_without_them_is_unplottable(self):
        """Its coverage is a radius about a point, and a radius about the
        wrong point is a circle over the wrong country."""
        assert navaid("ZZZ", NavaidKind.VOR).position is None

    def test_the_loader_reads_the_form_an_aip_prints(self, tmp_path, document):
        payload = manifest()
        payload["navaids"][0]["latitude"] = "251530N"
        payload["navaids"][0]["longitude"] = "0513015E"
        held = load_navaids(write(tmp_path, payload))
        assert held.navaid("ALP").position is not None

    def test_a_coordinate_cell_of_prose_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["navaids"][0]["longitude"] = "see chart"
        with pytest.raises(ManifestError, match="longitude"):
            load_navaids(write(tmp_path, payload))
