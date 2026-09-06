"""ENR 1.8 — where the Annex is not what you are flying under.

Three layers govern a flight and only the bottom one is in the crew's manual:
the Annex, the region's supplementary procedures, and the State's departures
from those. Somebody planning from the Annex alone is not slightly out of date
— they are reading a document that does not govern the airspace they are in.

The assertions are about four things.

**The finding is the boundary.** Six regions' procedures are a table nobody
reads. Where lateral offset stops being permitted is a place, and a place is
actionable.

**A boundary with one side unread is not one where nothing changes.** It is one
nobody can speak for, and it says so.

**Silence is never agreement.** A region nobody has read publishes nothing
here, and "publishes nothing" and "publishes that it follows the region" are
opposite answers.

**Two entries labelled "other" are not about the same thing.** Comparing them
would produce a finding about our own labelling.

Every region and procedure below is a fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef
from aeropub.supps import (
    Applicability,
    ProcedureArea,
    SuppsRegister,
    SupplementaryProcedure,
    load_supps,
    supps_template,
    view_supps,
)

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 1.8",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def procedure(region: str, area: ProcedureArea, **overrides) -> SupplementaryProcedure:
    fields = dict(region=region, area=area, source=ref())
    fields.update(overrides)
    return SupplementaryProcedure(**fields)


OFFSET_ALLOWED = procedure(
    "AAAA",
    ProcedureArea.LATERAL_OFFSET,
    icao_region="MID",
    applicability=Applicability.APPLIED,
    summary="SLOP up to 2 NM right of centreline",
    reference="Doc 7030 MID 3.2",
)
OFFSET_REFUSED = procedure(
    "BBBB",
    ProcedureArea.LATERAL_OFFSET,
    icao_region="MID",
    applicability=Applicability.NOT_APPLICABLE,
    summary="offsets are not permitted",
    reference="Doc 7030 MID 3.2",
)
CONTINGENCY_AAAA = procedure(
    "AAAA",
    ProcedureArea.CONTINGENCY,
    icao_region="MID",
    applicability=Applicability.APPLIED,
    summary="turn 45 degrees and offset 15 NM",
)
CONTINGENCY_BBBB = procedure(
    "BBBB",
    ProcedureArea.CONTINGENCY,
    icao_region="MID",
    applicability=Applicability.DIFFERS,
    summary="turn 90 degrees and offset 30 NM",
)


def register(*procedures, covers: tuple[str, ...] = ()) -> SuppsRegister:
    held = procedures or (
        OFFSET_ALLOWED,
        OFFSET_REFUSED,
        CONTINGENCY_AAAA,
        CONTINGENCY_BBBB,
    )
    return SuppsRegister(procedures=held, covers=frozenset(covers))


# --------------------------------------------------------------------------
# What a statement says
# --------------------------------------------------------------------------


class TestStatement:
    def test_differing_departs_from_the_region(self):
        assert Applicability.DIFFERS.departs_from_the_region is True

    def test_not_applicable_departs_too(self):
        """Published as not applying is itself a difference from a crew's
        assumption, and it is not silence."""
        assert Applicability.NOT_APPLICABLE.departs_from_the_region is True

    def test_applied_does_not(self):
        assert Applicability.APPLIED.departs_from_the_region is False

    def test_an_unstated_applicability_answers_neither(self):
        """Assuming either way is the failure this section exists to
        prevent."""
        assert Applicability.NOT_STATED.departs_from_the_region is None

    def test_a_procedure_with_no_airspace_is_refused(self):
        """A supplementary procedure is never global."""
        with pytest.raises(ValueError, match="region"):
            SupplementaryProcedure(
                region="", area=ProcedureArea.RVSM, source=ref()
            )

    def test_the_description_carries_the_states_own_words(self):
        assert "SLOP up to 2 NM" in OFFSET_ALLOWED.describe()

    def test_the_icao_region_is_held_as_printed(self):
        assert OFFSET_ALLOWED.icao_region == "MID"


class TestAreas:
    def test_other_is_never_compared(self):
        """Two entries both labelled "other" are not about the same thing."""
        assert not ProcedureArea.OTHER.is_comparable
        assert ProcedureArea.CONTINGENCY.is_comparable

    def test_the_label_reads_like_prose(self):
        assert ProcedureArea.LATERAL_OFFSET.label == "lateral offset"


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


class TestChanges:
    def test_a_procedure_that_changes_is_a_finding_at_the_boundary(self):
        found = view_supps(register(), regions=["AAAA", "BBBB"])
        offset = next(
            c for c in found.changes if c.area is ProcedureArea.LATERAL_OFFSET
        )
        assert offset.leaving == "AAAA" and offset.entering == "BBBB"
        assert offset.is_known
        assert "lateral offset changes" in offset.describe()

    def test_a_change_of_standing_is_reported_as_such(self):
        """Applied on one side and not applicable on the other: a crew
        following the regional text is right up to the boundary."""
        found = view_supps(register(), regions=["AAAA", "BBBB"])
        offset = next(
            c for c in found.changes if c.area is ProcedureArea.LATERAL_OFFSET
        )
        assert offset.changes_applicability

    def test_the_entering_procedure_is_quoted(self):
        found = view_supps(register(), regions=["AAAA", "BBBB"])
        text = next(
            c.describe()
            for c in found.changes
            if c.area is ProcedureArea.CONTINGENCY
        )
        assert "turn 90 degrees and offset 30 NM" in text

    def test_the_same_procedure_either_side_is_not_a_change(self):
        held = register(
            CONTINGENCY_AAAA,
            procedure(
                "BBBB",
                ProcedureArea.CONTINGENCY,
                applicability=Applicability.APPLIED,
                summary="turn 45 degrees and offset 15 NM",
            ),
        )
        assert view_supps(held, regions=["AAAA", "BBBB"]).changes == ()

    def test_the_same_standing_with_different_words_is_still_a_change(self):
        """The words are what a crew acts on."""
        held = register(
            CONTINGENCY_AAAA,
            procedure(
                "BBBB",
                ProcedureArea.CONTINGENCY,
                applicability=Applicability.APPLIED,
                summary="turn 90 degrees and offset 30 NM",
            ),
        )
        found = view_supps(held, regions=["AAAA", "BBBB"])
        assert len(found.changes) == 1
        assert not found.changes[0].changes_applicability

    def test_a_boundary_with_one_side_unread_cannot_be_spoken_for(self):
        held = register(OFFSET_ALLOWED)  # BBBB never declared, never read
        found = view_supps(held, regions=["AAAA", "BBBB"])
        offset = found.changes[0]
        assert not offset.is_speakable
        assert "nobody can say whether it changes" in offset.describe()
        assert offset in found.unspeakable_boundaries
        assert offset not in found.known_changes

    def test_a_read_but_silent_side_is_an_answer_not_a_gap(self):
        """The procedure stops being published, and a crew carrying it across
        the boundary is carrying something the next State did not publish."""
        held = register(OFFSET_ALLOWED, covers=("BBBB",))
        found = view_supps(held, regions=["AAAA", "BBBB"])
        change = found.changes[0]
        assert change.is_speakable and not change.is_known
        assert change in found.one_sided
        assert change not in found.unspeakable_boundaries
        assert "was read and publishes nothing for it" in change.describe()

    def test_the_render_keeps_the_two_absences_apart(self):
        """One side read and silent, and one side never read, are different
        sections of the report."""
        held = SuppsRegister(
            procedures=(OFFSET_ALLOWED, CONTINGENCY_BBBB), covers=frozenset()
        )
        page = view_supps(held, regions=["AAAA", "BBBB", "ZZZZ"]).render()
        assert "PUBLISHED ON ONE SIDE ONLY" in page
        assert "BOUNDARIES NOBODY CAN SPEAK FOR" in page

    def test_an_unread_region_is_reported_once_not_once_per_area(self):
        """Twelve identical findings all saying "read ZZZZ" is noise, and the
        region is already named as unread."""
        found = view_supps(register(), regions=["BBBB", "ZZZZ"])
        assert found.unread_regions == ("ZZZZ",)
        # Only the areas BBBB actually publishes reach a boundary finding.
        assert {c.area for c in found.unspeakable_boundaries} == {
            ProcedureArea.LATERAL_OFFSET,
            ProcedureArea.CONTINGENCY,
        }

    def test_an_area_neither_side_mentions_is_not_a_boundary_finding(self):
        found = view_supps(register(), regions=["AAAA", "BBBB"])
        assert not any(c.area is ProcedureArea.RVSM for c in found.changes)

    def test_other_never_produces_a_boundary_finding(self):
        held = register(
            procedure("AAAA", ProcedureArea.OTHER, summary="one thing"),
            procedure("BBBB", ProcedureArea.OTHER, summary="another thing"),
        )
        assert view_supps(held, regions=["AAAA", "BBBB"]).changes == ()

    def test_the_order_of_the_regions_is_the_order_of_overflight(self):
        forward = view_supps(register(), regions=["AAAA", "BBBB"]).changes[0]
        backward = view_supps(register(), regions=["BBBB", "AAAA"]).changes[0]
        assert (forward.leaving, forward.entering) == ("AAAA", "BBBB")
        assert (backward.leaving, backward.entering) == ("BBBB", "AAAA")

    def test_a_region_listed_twice_running_makes_no_boundary_with_itself(self):
        found = view_supps(register(), regions=["AAAA", "AAAA", "BBBB"])
        assert all(c.leaving != c.entering for c in found.changes)

    def test_the_comparison_can_be_narrowed_to_one_area(self):
        found = view_supps(
            register(),
            regions=["AAAA", "BBBB"],
            areas=[ProcedureArea.CONTINGENCY],
        )
        assert {c.area for c in found.changes} == {ProcedureArea.CONTINGENCY}


# --------------------------------------------------------------------------
# What was read
# --------------------------------------------------------------------------


class TestCoverage:
    def test_a_region_nobody_read_is_named(self):
        found = view_supps(register(), regions=["AAAA", "ZZZZ"])
        assert found.unread_regions == ("ZZZZ",)
        assert not found.is_conclusive

    def test_a_region_declared_read_with_nothing_in_it_is_not_unread(self):
        """Following the region exactly and never having been read are
        opposite answers to whether the Annex is enough."""
        held = register(OFFSET_ALLOWED, covers=("CCCC",))
        found = view_supps(held, regions=["CCCC"])
        assert found.unread_regions == ()
        assert found.is_conclusive

    def test_the_render_says_the_annex_question_is_unanswered(self):
        page = view_supps(register(), regions=["ZZZZ"]).render()
        assert "ZZZZ" in page
        assert "not something the held documents answer" in page

    def test_the_differences_are_collected(self):
        found = view_supps(register(), regions=["AAAA", "BBBB"])
        assert {p.region for p in found.differences} == {"BBBB"}

    def test_an_unstated_applicability_is_not_counted_as_a_difference(self):
        held = register(procedure("AAAA", ProcedureArea.RVSM))
        assert view_supps(held, regions=["AAAA"]).differences == ()

    def test_an_empty_region_matches_nothing(self):
        assert register().in_region("") == ()

    def test_one_area_in_one_region_is_addressable(self):
        found = register().at("AAAA", ProcedureArea.CONTINGENCY)
        assert found is CONTINGENCY_AAAA
        assert register().at("AAAA", ProcedureArea.RVSM) is None


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr18.txt"
    path.write_text(
        "an ENR 1.8 paragraph, standing in for one somebody read\n",
        encoding="utf-8",
    )
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "enr18.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP AA ENR 1.8",
            "document_path": "enr18.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "icao_region": "MID",
        "covers": ["BBBB"],
        "procedures": [
            {
                "area": "lateral offset",
                "applicability": "applied",
                "summary": "SLOP up to 2 NM right of centreline",
                "reference": "Doc 7030 MID 3.2",
                "locator": "ENR 1.8 para 3.2",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_statement_cited(self, tmp_path, document):
        held = load_supps(write(tmp_path, manifest()))
        found = held.in_region("AAAA")[0]
        assert found.source.locator == "ENR 1.8 para 3.2"
        assert found.area is ProcedureArea.LATERAL_OFFSET

    def test_an_area_written_with_spaces_reads(self, tmp_path, document):
        """A person transcribing a page writes "lateral offset"."""
        held = load_supps(write(tmp_path, manifest()))
        assert held.at("AAAA", ProcedureArea.LATERAL_OFFSET) is not None

    def test_the_icao_region_defaults_to_the_extract(self, tmp_path, document):
        held = load_supps(write(tmp_path, manifest()))
        assert held.in_region("AAAA")[0].icao_region == "MID"

    def test_the_declared_regions_are_read_even_with_no_rows(self, tmp_path, document):
        held = load_supps(write(tmp_path, manifest()))
        assert held.is_read("BBBB")
        assert held.in_region("BBBB") == ()

    def test_a_row_without_a_locator_is_refused(self, tmp_path, document):
        payload = manifest()
        del payload["procedures"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_supps(write(tmp_path, payload))

    def test_an_unknown_area_is_refused_rather_than_freetext(self, tmp_path, document):
        payload = manifest()
        payload["procedures"][0]["area"] = "something the State invented"
        with pytest.raises(ManifestError, match="both sides mean the same"):
            load_supps(write(tmp_path, payload))

    def test_a_missing_applicability_is_not_stated(self, tmp_path, document):
        payload = manifest()
        del payload["procedures"][0]["applicability"]
        held = load_supps(write(tmp_path, payload))
        assert (
            held.in_region("AAAA")[0].applicability is Applicability.NOT_STATED
        )

    def test_covers_must_be_a_list(self, tmp_path, document):
        with pytest.raises(ManifestError, match="covers"):
            load_supps(write(tmp_path, manifest(covers="BBBB")))

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(supps_template())
        assert blank["covers"] == []
        assert blank["procedures"][0]["applicability"] == "not_stated"
