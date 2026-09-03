"""What is in force, on what, at a given minute.

Provenance in these tests comes from a real archive entry over real bytes — the
AIXM the FAA issued — so a citation asserted here resolves the same way it does
in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aeropub.archive import Archive
from aeropub.notam import NotamKind
from aeropub.notam_register import (
    ForceState,
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "nms-initial-load-sample.raw"
RETRIEVED = datetime(2025, 9, 12, 17, 25, tzinfo=timezone.utc)

START = datetime(2025, 8, 21, 2, 34, tzinfo=timezone.utc)
END = datetime(2025, 10, 1, 23, 59, tzinfo=timezone.utc)


@pytest.fixture
def source(tmp_path):
    archive = Archive(tmp_path / "raw")
    entry = archive.put(
        FIXTURE.read_bytes(),
        source_id="FAA-NMS-PROD",
        url="https://api-nms.aim.faa.gov/nmsapi/v1/notams/il",
        retrieved_at=RETRIEVED,
    )
    return entry.to_source_ref(
        document="FAA NMS NOTAM STL 08/430",
        locator="AIXMBasicMessage NMS_ID_1757609538792382",
        parser_id="faa-nms-aixm",
        parser_version="0.1.0",
    )


def _notam(source, **overrides) -> RegisteredNotam:
    fields = dict(
        identifier="STL 08/430",
        subjects=(
            Subject(entity="8WC", kind=SubjectKind.AERODROME, designator="8WC"),
            Subject(entity="8WC/RWY20", kind=SubjectKind.RUNWAY_DIRECTION, designator="20"),
        ),
        source=source,
        text="RWY 20 RWY END ID LGT U/S",
        effective_start=START,
        effective_end=END,
        kind=NotamKind.NEW,
    )
    fields.update(overrides)
    return RegisteredNotam(**fields)


class TestConstruction:
    def test_provenance_is_required(self, source):
        with pytest.raises(TypeError, match="cannot be cited"):
            RegisteredNotam(identifier="X 01/001", subjects=(), source="a document")

    def test_an_identifier_is_required(self, source):
        with pytest.raises(ValueError, match="identifier"):
            _notam(source, identifier="  ")

    def test_naive_timestamps_are_refused(self, source):
        # An ambiguous NOTAM start is worse than no NOTAM start: it reads as
        # precise and is wrong by up to a day either way.
        with pytest.raises(ValueError, match="timezone-aware"):
            _notam(source, effective_start=datetime(2025, 8, 21, 2, 34))

    def test_an_entity_cannot_be_blank(self):
        with pytest.raises(ValueError, match="entity"):
            Subject(entity="   ", kind=SubjectKind.AERODROME)


class TestForceState:
    def test_before_during_and_after_the_window(self, source):
        notam = _notam(source)
        assert notam.state_at(START - timedelta(minutes=1)) is ForceState.NOT_YET
        assert notam.state_at(START) is ForceState.IN_FORCE
        assert notam.state_at(END) is ForceState.IN_FORCE
        assert notam.state_at(END + timedelta(minutes=1)) is ForceState.EXPIRED

    def test_minute_precision_is_kept(self, source):
        # The whole reason NOTAM are not date-precision facts. A runway
        # unserviceable from 02:34 was serviceable at 02:00, and a model that
        # rounded to the day would over-claim for two and a half hours.
        notam = _notam(source)
        assert notam.state_at(datetime(2025, 8, 21, 2, 0, tzinfo=timezone.utc)) is ForceState.NOT_YET
        assert notam.state_at(datetime(2025, 8, 21, 2, 34, tzinfo=timezone.utc)) is ForceState.IN_FORCE

    def test_a_schedule_is_never_reported_as_in_force(self, source):
        # "Daily:1100-0001" leaves the NOTAM dormant for eleven hours inside
        # its own window. Reporting it in force throughout is wrong in the
        # direction that gets someone airborne on a false assumption.
        notam = _notam(source, schedule="Daily:1100-0001~DLY 1100-0001")
        assert notam.has_schedule
        state = notam.state_at(datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc))
        assert state is ForceState.SCHEDULE_UNKNOWN
        assert state is not ForceState.IN_FORCE

    def test_an_unread_schedule_still_demands_attention(self, source):
        # Not knowing when it applies is a reason to read the NOTAM, never a
        # reason to drop it from a briefing.
        assert ForceState.SCHEDULE_UNKNOWN.is_operative
        assert ForceState.UNKNOWN.is_operative
        assert not ForceState.NOT_YET.is_operative
        assert not ForceState.EXPIRED.is_operative

    def test_a_schedule_outside_the_window_still_expires(self, source):
        notam = _notam(source, schedule="Daily:1100-0001")
        assert notam.state_at(END + timedelta(days=1)) is ForceState.EXPIRED

    def test_permanent_never_expires(self, source):
        notam = _notam(source, permanent=True, effective_end=None)
        assert notam.state_at(datetime(2030, 1, 1, tzinfo=timezone.utc)) is ForceState.IN_FORCE

    def test_an_open_ended_window_never_expires(self, source):
        notam = _notam(source, effective_end=None)
        assert notam.state_at(datetime(2030, 1, 1, tzinfo=timezone.utc)) is ForceState.IN_FORCE

    def test_no_start_is_unknown_not_assumed_either_way(self, source):
        notam = _notam(source, effective_start=None)
        assert notam.state_at(datetime(2025, 9, 1, tzinfo=timezone.utc)) is ForceState.UNKNOWN

    def test_a_naive_moment_is_refused(self, source):
        with pytest.raises(ValueError, match="timezone-aware"):
            _notam(source).state_at(datetime(2025, 9, 1))


class TestSubjectRollUp:
    def test_an_aerodrome_query_reaches_its_runways(self, source):
        notam = _notam(source)
        assert notam.affects("8WC")
        assert notam.affects("8WC/RWY20")

    def test_a_runway_query_does_not_reach_the_whole_aerodrome(self, source):
        # Roll-up in one direction only. An apron closure filed against the
        # aerodrome must not surface as a finding about a runway.
        notam = RegisteredNotam(
            identifier="STL 08/431",
            subjects=(Subject(entity="8WC", kind=SubjectKind.AERODROME),),
            source=source,
            effective_start=START,
            effective_end=END,
        )
        assert notam.affects("8WC")
        assert not notam.affects("8WC/RWY20")

    def test_a_prefix_that_is_not_a_path_segment_does_not_match(self, source):
        notam = _notam(source)
        assert not notam.affects("8W")
        assert not notam.affects("8WC/RWY2")

    def test_the_aerodrome_part_is_recoverable_from_a_key(self):
        assert Subject(entity="8WC/RWY02/20", kind=SubjectKind.RUNWAY).aerodrome == "8WC"
        assert Subject(entity="8WC", kind=SubjectKind.AERODROME).aerodrome == "8WC"


class TestAttribution:
    def test_a_filed_location_is_not_a_structural_attribution(self, source):
        # Knowing a message concerns ZBW is real information. It is not the
        # same information as knowing which runway it closes, and a dossier
        # must not present them alike.
        filed = RegisteredNotam(
            identifier="BDR 04/221",
            subjects=(Subject(entity="KZBW", kind=SubjectKind.FILED_LOCATION, designator="ZBW"),),
            source=source,
            effective_start=START,
        )
        assert not filed.is_structurally_attributed
        assert not SubjectKind.FILED_LOCATION.is_structural
        assert "affected object not stated" in filed.subjects[0].describe()

    def test_structural_kinds_report_themselves_as_such(self):
        for kind in SubjectKind:
            if kind is SubjectKind.FILED_LOCATION:
                continue
            assert kind.is_structural, kind


class TestRegister:
    def test_indexes_by_every_subject(self, source):
        register = NotamRegister([_notam(source)])
        assert register.entities() == {"8WC", "8WC/RWY20"}
        assert len(register.for_entity("8WC")) == 1
        assert len(register.for_entity("8WC/RWY20")) == 1

    def test_a_replacement_does_not_erase_what_it_replaced(self, source):
        # Superseding belongs to the archive and the fact store. A register
        # that dropped the earlier message would make the replacement look
        # like the only one ever issued.
        register = NotamRegister([
            _notam(source),
            _notam(source, kind=NotamKind.REPLACE),
        ])
        assert len(register) == 2

    def test_at_pairs_each_notam_with_its_state(self, source):
        register = NotamRegister([_notam(source, schedule="Daily:1100-0001")])
        rows = register.at("8WC", datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc))
        assert [state for _, state in rows] == [ForceState.SCHEDULE_UNKNOWN]

    def test_only_certainly_in_force_can_be_asked_for_separately(self, source):
        register = NotamRegister([_notam(source, schedule="Daily:1100-0001")])
        moment = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)
        assert register.at("8WC", moment, include_unresolved=False) == ()
        assert len(register.at("8WC", moment)) == 1

    def test_an_expired_notam_is_not_returned(self, source):
        register = NotamRegister([_notam(source)])
        assert register.at("8WC", datetime(2026, 1, 1, tzinfo=timezone.utc)) == ()

    def test_aerodromes_excludes_filed_only_locations(self, source):
        register = NotamRegister([
            _notam(source),
            RegisteredNotam(
                identifier="BDR 04/221",
                subjects=(Subject(entity="KZBW", kind=SubjectKind.FILED_LOCATION),),
                source=source,
                effective_start=START,
            ),
        ])
        assert register.aerodromes() == {"8WC"}
        assert [n.identifier for n in register.unattributed()] == ["BDR 04/221"]

    def test_coverage_counts_what_could_not_be_attributed(self, source):
        register = NotamRegister([
            _notam(source, schedule="Daily:1100-0001", estimated=True),
            RegisteredNotam(
                identifier="BDR 04/221",
                subjects=(Subject(entity="KZBW", kind=SubjectKind.FILED_LOCATION),),
                source=source,
                effective_start=START,
            ),
        ])
        assert register.coverage() == {
            "notams": 2,
            "entities": 3,
            "structurally_attributed": 1,
            "filed_location_only": 1,
            "with_schedule": 1,
            "estimated_end": 1,
        }


class TestRendering:
    def test_an_unindexed_entity_reads_as_a_coverage_gap(self, source):
        # The dangerous failure this project exists to avoid: an aerodrome we
        # never checked looking exactly like one with nothing wrong.
        register = NotamRegister([_notam(source)])
        rendered = register.render("KJFK", datetime(2025, 9, 1, tzinfo=timezone.utc))
        assert "coverage gap" in rendered
        assert "not a quiet aerodrome" in rendered

    def test_an_indexed_entity_with_nothing_in_force_says_so_differently(self, source):
        register = NotamRegister([_notam(source)])
        rendered = register.render("8WC", datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert "no NOTAM in force" in rendered
        assert "coverage gap" not in rendered

    def test_a_briefing_carries_the_text_the_schedule_and_the_citation(self, source):
        register = NotamRegister([_notam(source, schedule="Daily:1100-0001")])
        rendered = register.render("8WC", datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc))
        assert "RWY 20 RWY END ID LGT U/S" in rendered
        assert "schedule: Daily:1100-0001" in rendered
        assert "schedule_unknown" in rendered
        assert "faa-nms-aixm" in rendered
