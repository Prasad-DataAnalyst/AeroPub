"""The entity key grammar.

This module exists because the rule it holds was previously written out at four
call sites, and they had drifted: two normalised case and two did not, so
``register.at("8wc")`` returned nothing for an aerodrome with a live runway
NOTAM — and ``register.render("8wc")`` reported it as a coverage gap. A
confident "nothing here" about somewhere that has something is the exact
failure this project is built to avoid, so the regression is asserted below
against the real FAA fixture as well as against the grammar itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.entities import (
    APRON,
    RUNWAY,
    TAXIWAY,
    aerodrome_of,
    beneath,
    compose,
    covers,
    is_free_standing,
    normalise,
    scope_of,
)

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "nms-initial-load-sample.raw"


class TestContainment:
    @pytest.mark.parametrize(
        "parent,key,expected",
        [
            ("OTHH", "OTHH", True),
            ("OTHH", "OTHH/RWY34L", True),
            ("8WC", "8WC/RWY02/20", True),
            ("OTHH/RWY34L", "OTHH/RWY34L", True),
            # One-directional: a runway query must not reach the aerodrome.
            ("OTHH/RWY34L", "OTHH", False),
            # A prefix that is not a segment boundary is a different aerodrome.
            ("OTHH", "OTHHX", False),
            ("8W", "8WC", False),
            ("OTH", "OTHH/RWY34L", False),
        ],
    )
    def test_the_rule(self, parent, key, expected):
        assert covers(parent, key) is expected

    def test_case_and_whitespace_never_decide_the_answer(self):
        # The bug. Two spellings of one aerodrome are one aerodrome.
        for spelling in ("8wc", " 8WC ", "8Wc"):
            assert covers(spelling, "8WC/RWY20")
            assert covers("8WC", spelling)

    def test_beneath_preserves_order_and_filters(self):
        keys = ["OTHH", "OTBD", "OTHH/RWY34L", "OTHHX", "othh/rwy16r"]
        assert list(beneath("OTHH", keys)) == ["OTHH", "OTHH/RWY34L", "othh/rwy16r"]


class TestDecomposition:
    @pytest.mark.parametrize(
        "key,aerodrome,scope",
        [
            ("OTHH", "OTHH", None),
            ("OTHH/RWY34L", "OTHH", "RWY34L"),
            # Only the first separator divides — a runway pair contains one.
            ("8WC/RWY02/20", "8WC", "RWY02/20"),
            ("AIRSPACE:EGTT", None, None),
        ],
    )
    def test_a_key_splits_into_aerodrome_and_scope(self, key, aerodrome, scope):
        assert aerodrome_of(key) == aerodrome
        assert scope_of(key) == scope

    def test_a_free_standing_object_belongs_to_no_aerodrome(self):
        # Rolling airspace up under an aerodrome would attribute a danger area
        # to a runway.
        assert is_free_standing("AIRSPACE:EGTT")
        assert is_free_standing("ROUTE:UL607")
        assert not is_free_standing("OTHH")
        assert not is_free_standing("OTHH/RWY34L")

    def test_a_colon_after_a_separator_is_not_a_kind_prefix(self):
        assert not is_free_standing("OTHH/STAND:A12")
        assert aerodrome_of("OTHH/STAND:A12") == "OTHH"


class TestComposition:
    def test_builds_the_conventional_key(self):
        assert compose("OTHH", RUNWAY, "34L") == "OTHH/RWY34L"
        assert compose("OTHH", TAXIWAY, "A7") == "OTHH/TWYA7"
        assert compose("OTHH", APRON, "3") == "OTHH/APRON3"

    def test_normalises_both_halves(self):
        assert compose(" othh ", RUNWAY, " 34l ") == "OTHH/RWY34L"

    def test_refuses_a_key_with_an_empty_half(self):
        # "RWY20" alone names a runway at every aerodrome that has one.
        with pytest.raises(ValueError, match="no aerodrome"):
            compose("", RUNWAY, "20")
        with pytest.raises(ValueError, match="no designator"):
            compose("OTHH", RUNWAY, "  ")

    def test_a_composed_key_decomposes_back(self):
        key = compose("OTHH", RUNWAY, "34L")
        assert aerodrome_of(key) == "OTHH"
        assert scope_of(key) == "RWY34L"
        assert covers("OTHH", key)


class TestNormalise:
    def test_trims_uppercases_and_collapses(self):
        assert normalise("  othh  ") == "OTHH"
        assert normalise("AD  2.13") == "AD 2.13"


class TestRegression:
    """The lookup that used to return a confident 'nothing here'."""

    @pytest.fixture
    def register(self, tmp_path):
        from aeropub.archive import Archive
        from aeropub.faa.aixm import read_notams
        from aeropub.faa.register import register_feed

        archive = Archive(tmp_path / "raw")
        entry = archive.put(
            FIXTURE.read_bytes(), source_id="FAA-NMS-PROD",
            url="https://api-nms.aim.faa.gov/nmsapi/v1/notams/il",
            retrieved_at=datetime(2025, 9, 12, 17, 25, tzinfo=timezone.utc),
        )
        return register_feed(read_notams(str(FIXTURE)), entry)

    @pytest.mark.parametrize("spelling", ["8WC", "8wc", " 8Wc ", "8WC/RWY20", "8wc/rwy20"])
    def test_every_spelling_finds_the_live_notam(self, register, spelling):
        moment = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)
        assert len(register.at(spelling, moment)) == 1
        assert len(register.for_entity(spelling)) == 1

    def test_a_lower_case_query_is_not_reported_as_a_coverage_gap(self, register):
        moment = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)
        rendered = register.render("8wc", moment)
        assert "coverage gap" not in rendered
        assert "STL 08/430" in rendered

    def test_the_dossier_and_the_register_agree(self, register):
        # They disagreed: build() normalised and the register did not, so the
        # same aerodrome had two answers depending on which door you came in.
        from aeropub.dossier import build

        moment = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)
        for spelling in ("8WC", "8wc"):
            dossier = build(spelling, register=register, as_at=moment)
            assert len(dossier.notams) == len(register.at(spelling, moment)) == 1

    def test_subjects_are_normalised_at_construction(self):
        from aeropub.notam_register import Subject, SubjectKind

        assert Subject(entity=" 8wc ", kind=SubjectKind.AERODROME).entity == "8WC"
