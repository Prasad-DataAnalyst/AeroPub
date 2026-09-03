"""Mapping FAA AIXM onto canonical entity keys.

Against the real sample the FAA issued: one runway-light NOTAM at Washington
County with three linked features, and one UAS airspace notice in Boston Center
with none at all. Between them they cover both halves of the mapping — the case
where the source says what is affected, and the case where it does not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.archive import Archive
from aeropub.faa.aixm import AffectedFeature, NotamFeed, read_notams
from aeropub.faa.register import FEATURE_KINDS, register_feed, registered, subjects_of
from aeropub.notam_register import ForceState, SubjectKind

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "nms-initial-load-sample.raw"
RETRIEVED = datetime(2025, 9, 12, 17, 25, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def notams():
    return read_notams(str(FIXTURE))


@pytest.fixture
def entry(tmp_path):
    archive = Archive(tmp_path / "raw")
    return archive.put(
        FIXTURE.read_bytes(),
        source_id="FAA-NMS-PROD",
        url="https://api-nms.aim.faa.gov/nmsapi/v1/notams/il",
        retrieved_at=RETRIEVED,
    )


@pytest.fixture
def register(notams, entry):
    return register_feed(notams, entry)


class TestKeys:
    def test_a_runway_notam_keys_to_aerodrome_runway_and_direction(self, notams):
        subjects = {s.entity: s for s in subjects_of(notams[0])}
        assert set(subjects) == {"8WC", "8WC/RWY02/20", "8WC/RWY20"}
        assert subjects["8WC"].kind is SubjectKind.AERODROME
        assert subjects["8WC/RWY02/20"].kind is SubjectKind.RUNWAY
        assert subjects["8WC/RWY20"].kind is SubjectKind.RUNWAY_DIRECTION

    def test_keys_follow_the_fact_stores_convention(self, notams):
        # OTHH/RWY34L in the fact store; 8WC/RWY20 here. One convention, so a
        # NOTAM and a declared distance can be about the same runway.
        assert all("/" not in s.entity or s.entity.startswith("8WC/")
                   for s in subjects_of(notams[0]))
        assert subjects_of(notams[0])[0].aerodrome == "8WC"

    def test_runway_geometry_is_not_indexed_as_a_separate_object(self, notams):
        # RunwayElement carries a runway's extent, not another object, and has
        # no designator. Indexing it would double-count every runway NOTAM
        # under a key made of a UUID.
        assert "RunwayElement" in {f.kind for f in notams[0].features}
        assert "RunwayElement" not in FEATURE_KINDS
        assert all(s.uuid != "cee68773-faf6-43a4-8314-cf4dd8cd5fa4"
                   for s in subjects_of(notams[0]))

    def test_the_stable_identifier_is_carried_through(self, notams):
        aerodrome = {s.entity: s for s in subjects_of(notams[0])}["8WC"]
        assert aerodrome.uuid == "b7a0209e-942f-4d7e-ae8b-c708fce65328"

    def test_an_unmapped_feature_type_falls_back_rather_than_inventing_a_key(self, notams):
        # A feature type we have no convention for produces no subject of its
        # own — an entity key nobody can look up is worse than an admitted gap
        # — so the NOTAM lands on the filed-location fallback instead.
        from dataclasses import replace

        invented = replace(notams[0], features=(
            AffectedFeature(kind="SomethingTheFAAAddedLater", designator="X1"),
        ))
        assert subjects_of(invented)[0].kind is SubjectKind.FILED_LOCATION

    def test_a_runway_with_no_aerodrome_cannot_be_keyed(self):
        # "RWY20" alone names a runway at every aerodrome that has one.
        from dataclasses import replace

        from aeropub.faa.aixm import NmsNotam

        orphan = NmsNotam(
            nms_id="X", uuid=None, number=1, year=2025, kind=None,
            location=None, icao_location=None,
            features=(AffectedFeature(kind="RunwayDirection", designator="20"),),
        )
        assert subjects_of(orphan) == ()


class TestFiledLocationFallback:
    def test_an_airspace_notam_with_no_linked_feature_records_where_it_was_filed(
        self, notams
    ):
        subjects = subjects_of(notams[1])
        assert len(subjects) == 1
        assert subjects[0].kind is SubjectKind.FILED_LOCATION
        assert subjects[0].entity == "KZBW"
        assert subjects[0].designator == "ZBW"

    def test_the_icao_indicator_is_preferred_where_the_faa_supplied_one(self, notams):
        assert notams[1].icao_location == "KZBW"
        assert subjects_of(notams[1])[0].entity == "KZBW"

    def test_a_three_character_identifier_is_not_promoted_to_icao(self, notams):
        # 8WC has no ICAO equivalent. Prefixing K would invent an aerodrome.
        aerodrome = {s.entity: s for s in subjects_of(notams[0])}["8WC"]
        assert aerodrome.icao is None
        assert aerodrome.entity == "8WC"


class TestRegisterFeed:
    def test_indexes_the_whole_feed_with_provenance(self, register, entry, tmp_path):
        assert len(register) == 2
        for notam in register:
            assert notam.source.content_hash == entry.digest
            assert notam.source.parser_id == "faa-nms-aixm"

    def test_coverage_separates_attributed_from_filed_only(self, register):
        coverage = register.coverage()
        assert coverage["notams"] == 2
        assert coverage["structurally_attributed"] == 1
        assert coverage["filed_location_only"] == 1
        assert coverage["with_schedule"] == 1

    def test_an_aerodrome_query_returns_its_runway_notam(self, register):
        moment = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)
        rows = register.at("8WC", moment)
        assert [n.identifier for n, _ in rows] == ["STL 08/430"]
        assert rows[0][1] is ForceState.IN_FORCE

    def test_the_scheduled_airspace_notam_is_not_claimed_in_force_at_0600(self, register):
        # Active daily 1100-0001, so at 06:00 it is dormant — but the schedule
        # has not been parsed, so neither yes nor no is honest.
        moment = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)
        rows = register.at("KZBW", moment)
        assert [state for _, state in rows] == [ForceState.SCHEDULE_UNKNOWN]
        assert register.at("KZBW", moment, include_unresolved=False) == ()

    def test_a_feed_can_be_accumulated_across_classifications(self, notams, entry):
        register = register_feed(notams, entry)
        register_feed(notams, entry, into=register)
        assert len(register) == 4

    def test_reading_from_a_streamed_feed_matches_reading_the_list(self, entry):
        feed = NotamFeed(str(FIXTURE))
        register = register_feed(feed, entry)
        assert len(register) == feed.notams_read == 2
        # The feed's own count is what tells a caller the download was short.
        assert feed.is_complete is False

    def test_the_briefing_cites_bytes_that_still_resolve(self, register, tmp_path):
        archive = Archive(tmp_path / "raw")
        rendered = register.render("8WC", datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc))
        assert "RWY 20 RWY END ID LGT U/S" in rendered
        notam = register.for_entity("8WC")[0]
        assert archive.get(notam.source.content_hash) == FIXTURE.read_bytes()
