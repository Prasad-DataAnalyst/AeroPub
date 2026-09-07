"""The layer between the amendment and the NOTAM, which nothing could see.

`facts.Precedence` has said since the beginning that a supplement outranks the
AIP. Every ENR module reads the AIP and is overridden by NOTAM, and none of
them had ever heard of a supplement — so a State that raises a danger area for
the summer publishes it in a SUP and the atlas keeps drawing the base AIP.

Four things carry this module.

**A supplement is a pointer, not a patch.** Nothing reads a value out of its
prose. It reopens the question, exactly as a NOTAM reopens a navaid's published
status, and the reader goes and reads it.

**An unread window is not an ended one.** A supplement whose dates nobody
transcribed is never treated as expired.

**Open-ended is its own answer.** "Until further notice" is in force and is not
a dated window, and a reader planning months out needs to know which.

**A replaced supplement is not shown.** The State said which is current.

Every identifier and date below is a fixture.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aeropub.entities import named
from aeropub.facts import Precedence
from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef
from aeropub.supplement import (
    ForcePeriod,
    Supersession,
    Supplement,
    SupplementRegister,
    load_supplements,
    supplement_template,
)

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
DAY = date(2026, 10, 5)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="SUP A05/26",
        retrieved_at=NOW,
        content_hash="c" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def sup(identifier: str, **overrides) -> Supplement:
    fields = dict(identifier=identifier, source=ref())
    fields.update(overrides)
    return Supplement(**fields)


CURRENT = sup(
    "A05/26",
    section="ENR 5.1",
    subjects=(named("AIRSPACE", "AR-7"),),
    effective_from=date(2026, 6, 1),
    effective_to=date(2026, 11, 30),
    supersession=Supersession.AMENDS,
    summary="AR-7 upper limit raised for the summer season",
)
OPEN_ENDED = sup(
    "A06/26",
    section="ENR 3.2",
    subjects=(named("ATS", "UM688"),),
    effective_from=date(2026, 9, 1),
    supersession=Supersession.REPLACES,
    summary="UM688 withdrawn between ALSEM and MIDLE until further notice",
)
UNDATED = sup(
    "A07/26",
    section="ENR 2.1",
    subjects=(named("AIRSPACE", "ALPHA TMA"),),
    summary="ALPHA TMA lateral limits revised",
)


def register(*supplements) -> SupplementRegister:
    return SupplementRegister(
        supplements=supplements or (CURRENT, OPEN_ENDED, UNDATED)
    )


# --------------------------------------------------------------------------
# When it applies
# --------------------------------------------------------------------------


class TestPeriod:
    def test_a_day_inside_the_window_is_in_force(self):
        assert CURRENT.state_on(DAY) is ForcePeriod.IN_FORCE

    def test_a_day_before_it_is_not_yet(self):
        assert CURRENT.state_on(date(2026, 5, 1)) is ForcePeriod.NOT_YET

    def test_a_day_after_it_is_expired(self):
        assert CURRENT.state_on(date(2026, 12, 25)) is ForcePeriod.EXPIRED

    def test_the_first_and_last_days_are_inside(self):
        assert CURRENT.state_on(date(2026, 6, 1)) is ForcePeriod.IN_FORCE
        assert CURRENT.state_on(date(2026, 11, 30)) is ForcePeriod.IN_FORCE

    def test_until_further_notice_is_its_own_answer(self):
        """A reader planning six months out needs to know nothing says when
        it stops."""
        assert OPEN_ENDED.state_on(DAY) is ForcePeriod.OPEN_ENDED
        assert ForcePeriod.OPEN_ENDED.applies is True

    def test_an_unread_window_is_never_an_ended_one(self):
        """Treating it as expired would quietly retire a supplement that is
        still in force."""
        assert UNDATED.state_on(DAY) is ForcePeriod.UNDATED
        assert ForcePeriod.UNDATED.applies is None

    def test_an_end_before_its_start_is_refused(self):
        with pytest.raises(ValueError, match="wrong column"):
            sup(
                "A08/26",
                effective_from=date(2026, 6, 1),
                effective_to=date(2026, 5, 1),
            )

    def test_a_supplement_with_no_identifier_is_refused(self):
        """One nobody can name is one nobody can supersede."""
        with pytest.raises(ValueError, match="identifier"):
            Supplement(identifier="", source=ref())

    def test_the_window_reads_like_a_person_wrote_it(self):
        assert "until further notice" in OPEN_ENDED.window()
        assert "no validity window read" in UNDATED.window()


# --------------------------------------------------------------------------
# What it reaches
# --------------------------------------------------------------------------


class TestReach:
    def test_it_reaches_the_object_it_names(self):
        found = register().at(named("AIRSPACE", "AR-7"), DAY)
        assert [s.identifier for s, _ in found] == ["A05/26"]

    def test_the_period_travels_with_it(self):
        _, period = register().at(named("AIRSPACE", "AR-7"), DAY)[0]
        assert period is ForcePeriod.IN_FORCE

    def test_an_undated_one_still_reaches_the_object(self):
        found = register().at(named("AIRSPACE", "ALPHA TMA"), DAY)
        assert [p for _, p in found] == [ForcePeriod.UNDATED]

    def test_an_undated_one_can_be_excluded_for_a_count(self):
        found = register().at(
            named("AIRSPACE", "ALPHA TMA"), DAY, include_undated=False
        )
        assert found == ()

    def test_a_prefix_is_not_a_match(self):
        """A supplement about AR-7 is not one about AR-70, and a lookup that
        thought otherwise would attach a restriction to the wrong area."""
        assert register().at(named("AIRSPACE", "AR-70"), DAY) == ()

    def test_an_expired_one_does_not_reach_anything(self):
        assert register().at(named("AIRSPACE", "AR-7"), date(2026, 12, 25)) == ()

    def test_an_empty_key_matches_nothing(self):
        assert register().at("", DAY) == ()

    def test_a_section_lookup_finds_one_naming_no_object(self):
        """A supplement may name no object at all and still replace ENR 5.1
        for a month, and a lookup by object would never find it."""
        held = register(
            sup(
                "A09/26",
                section="ENR 5.1",
                effective_from=date(2026, 1, 1),
                supersession=Supersession.REPLACES,
            )
        )
        assert held.at(named("AIRSPACE", "AR-7"), DAY) == ()
        assert [s.identifier for s, _ in held.for_section("ENR 5.1", DAY)] == [
            "A09/26"
        ]

    def test_a_section_lookup_is_not_case_sensitive(self):
        assert register().for_section("enr 5.1", DAY)

    def test_a_section_wide_one_can_be_found_without_knowing_the_section(self):
        """A dossier reaches supplements by object. One heading a whole
        section names none, so it has to be reachable from the absence."""
        held = register(
            sup(
                "A09/26",
                section="ENR 5.1",
                effective_from=date(2026, 1, 1),
                supersession=Supersession.REPLACES,
            ),
            CURRENT,
        )
        assert [s.identifier for s, _ in held.section_wide(DAY)] == ["A09/26"]

    def test_a_section_wide_one_that_has_expired_is_not_returned(self):
        held = register(
            sup(
                "A09/26",
                section="ENR 5.1",
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 2, 1),
            )
        )
        assert held.section_wide(DAY) == ()

    def test_an_undated_section_wide_one_is_returned(self):
        """Nothing read says it has ended, which is not the same as its
        having ended."""
        held = register(sup("A09/26", section="ENR 5.1"))
        assert [p for _, p in held.section_wide(DAY)] == [ForcePeriod.UNDATED]

    def test_one_naming_no_section_is_not_section_wide(self):
        assert register(sup("A10/26")).section_wide(DAY) == ()

    def test_one_naming_neither_is_a_visible_category(self):
        """Real documents in force whose reach nobody has established. A
        dossier that omitted them silently would read as complete."""
        held = register(sup("A10/26", summary="something nobody placed"))
        assert [s.identifier for s in held.unattached()] == ["A10/26"]


class TestSupersession:
    def test_a_replaced_supplement_is_not_shown(self):
        """The State said which is current, and showing both would show a
        document that has been withdrawn."""
        old = sup(
            "A01/26",
            subjects=(named("AIRSPACE", "AR-7"),),
            effective_from=date(2026, 1, 1),
        )
        new = sup(
            "A05/26",
            subjects=(named("AIRSPACE", "AR-7"),),
            effective_from=date(2026, 6, 1),
            replaces="A01/26",
        )
        held = register(old, new)
        assert [s.identifier for s, _ in held.at(named("AIRSPACE", "AR-7"), DAY)] == [
            "A05/26"
        ]

    def test_replacing_a_section_invalidates_what_was_read_from_it(self):
        assert Supersession.REPLACES.invalidates_held_values is True
        assert Supersession.AMENDS.invalidates_held_values is True

    def test_adding_something_leaves_held_values_standing(self):
        """Nothing held is wrong; something held is missing."""
        assert Supersession.ADDS.invalidates_held_values is False

    def test_an_unstated_supersession_answers_neither(self):
        """One that might replace a section is not one that certainly does
        not."""
        assert Supersession.NOT_STATED.invalidates_held_values is None

    def test_the_in_force_list_drops_the_replaced(self):
        old = sup("A01/26", effective_from=date(2026, 1, 1))
        new = sup("A05/26", effective_from=date(2026, 6, 1), replaces="A01/26")
        assert [s.identifier for s in register(old, new).in_force(DAY)] == [
            "A05/26"
        ]


class TestPrecedence:
    def test_it_sits_where_facts_always_said_it_did(self):
        from aeropub.supplement import PRECEDENCE

        assert PRECEDENCE is Precedence.SUP
        assert Precedence.AIP < Precedence.SUP < Precedence.NOTAM

    def test_the_module_reads_no_value_out_of_the_prose(self):
        """Transcribing "upper limit becomes FL500" into a number would be a
        derived value carrying a citation."""
        import aeropub.supplement as module

        source = " ".join((module.__doc__ or "").split())
        assert "pointer, not a patch" in source
        for banned in ("apply_to", "patch", "new_upper_ft", "resolved_value"):
            assert not hasattr(module, banned)


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "sup.txt"
    path.write_text(
        "an AIP Supplement, standing in for one somebody read\n", encoding="utf-8"
    )
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "sup.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP AA SUP A05/26",
            "document_path": "sup.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "supplements": [
            {
                "identifier": "A05/26",
                "section": "ENR 5.1",
                "subjects": ["AIRSPACE:AR-7"],
                "effective_from": "2026-06-01",
                "effective_to": "2026-11-30",
                "supersession": "amends",
                "summary": "AR-7 upper limit raised for the summer season",
                "locator": "page 1",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_entry_cited(self, tmp_path, document):
        held = load_supplements(write(tmp_path, manifest()))
        found = held.supplements[0]
        assert found.source.locator == "page 1"
        assert found.effective_from == date(2026, 6, 1)

    def test_the_subjects_reach_the_object(self, tmp_path, document):
        held = load_supplements(write(tmp_path, manifest()))
        assert held.at(named("AIRSPACE", "AR-7"), DAY)

    def test_a_row_without_a_locator_is_refused(self, tmp_path, document):
        payload = manifest()
        del payload["supplements"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_supplements(write(tmp_path, payload))

    def test_an_unknown_supersession_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["supplements"][0]["supersession"] = "rewrites"
        with pytest.raises(ManifestError, match="supersession"):
            load_supplements(write(tmp_path, payload))

    def test_an_empty_end_date_is_open_ended(self, tmp_path, document):
        payload = manifest()
        payload["supplements"][0]["effective_to"] = ""
        held = load_supplements(write(tmp_path, payload))
        assert held.supplements[0].state_on(DAY) is ForcePeriod.OPEN_ENDED

    def test_no_dates_at_all_is_undated_not_expired(self, tmp_path, document):
        payload = manifest()
        payload["supplements"][0]["effective_from"] = ""
        payload["supplements"][0]["effective_to"] = ""
        held = load_supplements(write(tmp_path, payload))
        assert held.supplements[0].state_on(DAY) is ForcePeriod.UNDATED

    def test_a_blank_date_is_an_absence_not_an_error(self, tmp_path, document):
        """A blank cell means "not given" everywhere else in these readers,
        and the templates emit blanks for what a State may leave empty."""
        payload = manifest()
        payload["supplements"][0]["effective_to"] = "   "
        held = load_supplements(write(tmp_path, payload))
        assert held.supplements[0].effective_to is None

    def test_a_date_that_is_not_a_date_is_still_refused(self, tmp_path, document):
        payload = manifest()
        payload["supplements"][0]["effective_from"] = "next summer"
        with pytest.raises(ManifestError, match="is not a date"):
            load_supplements(write(tmp_path, payload))

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(supplement_template())
        assert blank["supplements"][0]["subjects"] == []
        assert blank["supplements"][0]["supersession"] == "not_stated"
