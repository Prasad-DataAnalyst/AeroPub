"""Reading NOTAM out of the FAA's AIXM.

Every assertion here is against ``tests/fixtures/faa/nms-initial-load-sample.raw``
— real AIXM issued by the FAA with API registration, stored byte for byte. The
two NOTAM in it are genuine: an unserviceable runway end identifier light at
Washington County (8WC), and a UAS airspace notice in Boston Center.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.faa.aixm import (
    NmsNotam,
    NotamFeed,
    _parse_effective,
    _parse_iso,
    iter_notams,
    read_notams,
)
from aeropub.notam import NotamKind

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "nms-initial-load-sample.raw"
METADATA = FIXTURE.with_suffix(".json")


@pytest.fixture(scope="module")
def notams() -> tuple[NmsNotam, ...]:
    return read_notams(str(FIXTURE))


@pytest.fixture(scope="module")
def runway_light(notams) -> NmsNotam:
    return notams[0]


@pytest.fixture(scope="module")
def uas_airspace(notams) -> NmsNotam:
    return notams[1]


class TestFixtureIntegrity:
    def test_the_fixture_is_the_bytes_the_faa_issued(self):
        import hashlib

        meta = json.loads(METADATA.read_text())
        body = FIXTURE.read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["content_hash"]
        assert len(body) == meta["content_length"]

    def test_the_fixture_carries_no_credential(self):
        # The initial-load handover response does carry one — a signed URL —
        # which is why no such response is committed anywhere in this tree.
        body = FIXTURE.read_bytes().decode()
        for marker in ("X-Goog-Signature", "Authorization", "access_token", "client_secret"):
            assert marker not in body


class TestFeedHeader:
    def test_reads_the_wfs_counts(self):
        feed = NotamFeed(str(FIXTURE))
        list(feed)
        assert feed.header is not None
        assert feed.header.number_returned == 21468
        assert feed.header.timestamp == datetime(
            2025, 9, 12, 17, 24, 2, 17000, tzinfo=timezone.utc
        )

    def test_a_short_read_is_visible(self):
        # The fixture is an excerpt: the FAA's own header claims 21,468
        # messages and two are present. A reader that did not notice would
        # under-report an entire country's NOTAM and look like a quiet day.
        feed = NotamFeed(str(FIXTURE))
        list(feed)
        assert feed.messages_seen == 2
        assert feed.is_complete is False

    def test_completeness_is_unknown_when_the_feed_does_not_say(self, tmp_path):
        doc = tmp_path / "bare.xml"
        doc.write_bytes(
            b'<?xml version="1.0"?>'
            b'<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0"/>'
        )
        feed = NotamFeed(str(doc))
        list(feed)
        assert feed.is_complete is None


class TestNotamIdentity:
    def test_reads_the_message_and_feature_identifiers(self, runway_light):
        assert runway_light.nms_id == "NMS_ID_1757609538792382"
        assert runway_light.uuid == "28c2b867-2028-4d12-b43c-8c0bb0525532"

    def test_number_and_year_come_from_the_structured_fields(self, runway_light):
        assert runway_light.number == 430
        assert runway_light.year == 2025

    def test_the_printed_number_is_read_not_reconstructed(self, runway_light):
        # 08/430 — the leading pair is the month of issue. Deriving it from
        # `issued` works until a NOTAM issued on the 1st carries the previous
        # month's number, which is exactly when someone is searching for it.
        assert runway_light.domestic_number == "08/430"
        assert runway_light.accountability == "STL"
        assert runway_light.identifier == "STL 08/430"

    def test_the_notam_type_is_not_confused_with_the_translation_type(self, runway_light):
        # event:NOTAM has a `type` of N; its nested NOTAMTranslation has a
        # `type` of LOCAL_FORMAT. Both are called `type`.
        assert runway_light.kind is NotamKind.NEW
        assert runway_light.translation_type == "LOCAL_FORMAT"


class TestValidity:
    def test_the_effective_window_matches_the_printed_text(self, runway_light):
        # The printed tail reads 2508210234-2510012359. The AIXM elements say
        # the same thing in twelve digits rather than ten.
        assert runway_light.effective_start == datetime(
            2025, 8, 21, 2, 34, tzinfo=timezone.utc
        )
        assert runway_light.effective_end == datetime(
            2025, 10, 1, 23, 59, tzinfo=timezone.utc
        )
        assert "2508210234-2510012359" in (runway_light.simple_text or "")

    def test_the_event_validity_is_kept_separately(self, runway_light):
        assert runway_light.valid_from == datetime(2025, 8, 21, 2, 34, tzinfo=timezone.utc)
        assert runway_light.valid_to == datetime(2025, 10, 1, 23, 59, tzinfo=timezone.utc)

    def test_in_force_respects_the_window(self, runway_light):
        during = datetime(2025, 9, 1, tzinfo=timezone.utc)
        before = datetime(2025, 8, 1, tzinfo=timezone.utc)
        after = datetime(2025, 11, 1, tzinfo=timezone.utc)
        assert runway_light.is_in_force(during)
        assert not runway_light.is_in_force(before)
        assert not runway_light.is_in_force(after)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("202508210234", datetime(2025, 8, 21, 2, 34, tzinfo=timezone.utc)),
            ("2508210234", datetime(2025, 8, 21, 2, 34, tzinfo=timezone.utc)),
        ],
    )
    def test_both_date_widths_read_the_same_moment(self, raw, expected):
        # Twelve digits carry a four-digit year, ten carry the ICAO two-digit
        # form. Reading twelve with the ten-digit rule gives month 25 and a
        # silent None, which is how a whole feed loses its validity windows.
        moment, permanent, estimated = _parse_effective(raw)
        assert moment == expected
        assert not permanent and not estimated

    def test_perm_and_est_are_distinguished(self):
        assert _parse_effective("PERM") == (None, True, False)
        moment, permanent, estimated = _parse_effective("202510012359EST")
        assert moment == datetime(2025, 10, 1, 23, 59, tzinfo=timezone.utc)
        assert estimated and not permanent

    def test_an_unreadable_value_is_none_not_a_guess(self):
        assert _parse_effective("SOON") == (None, False, False)

    def test_iso_instants_tolerate_z_and_any_fraction(self):
        expected = datetime(2025, 8, 21, 2, 34, 44, 784000, tzinfo=timezone.utc)
        assert _parse_iso("2025-08-21T02:34:44.784Z") == expected
        assert _parse_iso("2025-08-21T02:34:44.784+00:00") == expected
        assert _parse_iso("2025-08-21T02:34:44Z") == datetime(
            2025, 8, 21, 2, 34, 44, tzinfo=timezone.utc
        )
        assert _parse_iso("") is None
        assert _parse_iso("not a date") is None


class TestFaaExtension:
    def test_reads_the_fnse_fields(self, runway_light):
        assert runway_light.classification == "DOM"
        assert runway_light.account_id == "STL"
        assert runway_light.airport_name == "WASHINGTON COUNTY"
        assert runway_light.last_updated == datetime(
            2025, 8, 21, 2, 34, tzinfo=timezone.utc
        )

    def test_an_icao_indicator_is_reported_only_when_supplied(
        self, runway_light, uas_airspace
    ):
        # 8WC is a three-character FAA identifier with no ICAO indicator at
        # all. Deriving one by prefixing K would invent an aerodrome.
        assert runway_light.location == "8WC"
        assert runway_light.icao_location is None
        assert uas_airspace.location == "ZBW"
        assert uas_airspace.icao_location == "KZBW"

    def test_classification_is_the_short_form(self, runway_light):
        # The payload says DOM; request paths take DOMESTIC. Conflating them
        # produces a filter that silently matches nothing.
        assert runway_light.classification == "DOM"
        assert not runway_light.is_international


class TestAffectedFeatures:
    def test_the_affected_runway_is_known_structurally(self, runway_light):
        # This is the whole reason to prefer AIXM over text. "RWY 20 RWY END ID
        # LGT U/S" does not say which aerodrome; the linked features do.
        kinds = {f.kind for f in runway_light.features}
        assert {"AirportHeliport", "Runway", "RunwayDirection"} <= kinds

        directions = [f for f in runway_light.features if f.kind == "RunwayDirection"]
        assert [f.designator for f in directions] == ["20"]

        runways = [f for f in runway_light.features if f.kind == "Runway"]
        assert [f.designator for f in runways] == ["02/20"]

    def test_the_aerodrome_carries_its_designator_name_and_position(self, runway_light):
        aerodrome = runway_light.aerodromes()[0]
        assert aerodrome.designator == "8WC"
        assert aerodrome.name == "WASHINGTON COUNTY"
        assert aerodrome.latitude == pytest.approx(37.92919525)
        assert aerodrome.longitude == pytest.approx(-90.7314840277778)

    def test_features_carry_the_uuid_that_survives_renumbering(self, runway_light):
        aerodrome = runway_light.aerodromes()[0]
        assert aerodrome.uuid == "b7a0209e-942f-4d7e-ae8b-c708fce65328"

    def test_an_airspace_notam_links_no_aerodrome(self, uas_airspace):
        # And the reader says so rather than attaching it to something nearby.
        assert uas_airspace.features == ()
        assert uas_airspace.aerodromes() == ()
        assert uas_airspace.runways() == ()


class TestSchedule:
    def test_a_daily_schedule_is_preserved_verbatim(self, uas_airspace):
        # "Daily:1100-0001~DLY 1100-0001" is two renderings of one schedule.
        # Kept as issued: a NOTAM active 11:00-00:01 daily is not active for
        # its whole effective window, and flattening that loses the point.
        assert uas_airspace.schedule == "Daily:1100-0001~DLY 1100-0001"
        assert uas_airspace.effective_start == datetime(
            2025, 5, 1, 11, 0, tzinfo=timezone.utc
        )
        assert uas_airspace.effective_end == datetime(
            2025, 11, 2, 0, 1, tzinfo=timezone.utc
        )


class TestIcaoBridge:
    def test_faa_domestic_format_is_not_forced_through_the_icao_parser(
        self, runway_light, uas_airspace
    ):
        # !STL 08/430 8WC ... has no Q-line and no lettered items. The ICAO
        # parser would rightly refuse it, and pretending otherwise would put a
        # half-read NOTAM in front of a crew.
        assert runway_light.to_icao_notam() is None
        assert uas_airspace.to_icao_notam() is None


class TestProvenance:
    def test_a_citation_resolves_to_the_archived_bytes(self, runway_light, tmp_path):
        from aeropub.archive import Archive

        archive = Archive(tmp_path / "raw")
        body = FIXTURE.read_bytes()
        entry = archive.put(
            body,
            source_id="FAA-NMS-PROD",
            url="https://api-nms.aim.faa.gov/nmsapi/v1/notams/il",
            retrieved_at=datetime(2025, 9, 12, 17, 25, tzinfo=timezone.utc),
        )

        ref = runway_light.source_ref(entry)
        assert ref.content_hash == entry.digest
        assert archive.get(ref.content_hash) == body
        assert ref.parser_id == "faa-nms-aixm"
        assert "NMS_ID_1757609538792382" in ref.locator
        assert "STL 08/430" in ref.document


class TestStreaming:
    def test_reads_from_a_file_object_as_well_as_a_path(self):
        with FIXTURE.open("rb") as handle:
            assert len(list(iter_notams(handle))) == 2

    def test_memory_is_released_as_messages_are_consumed(self):
        # The container is emptied after each message, so the parse tree does
        # not grow with a twenty-thousand-message load. Asserted by observing
        # that iteration works from a stream and yields each message once.
        stream = io.BytesIO(FIXTURE.read_bytes())
        seen = [n.identifier for n in iter_notams(stream)]
        assert seen == ["STL 08/430", "BDR 04/221"]

    def test_a_message_without_an_event_is_counted_not_dropped(self, tmp_path):
        doc = tmp_path / "features-only.xml"
        doc.write_bytes(
            b'<?xml version="1.0"?>'
            b'<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" numberReturned="1">'
            b'<member xmlns="http://www.aixm.aero/schema/5.1"><AIXMBasicMessage '
            b'xmlns="http://www.aixm.aero/schema/5.1/message" gml:id="M1" '
            b'xmlns:gml="http://www.opengis.net/gml/3.2"/></member>'
            b"</FeatureCollection>"
        )
        feed = NotamFeed(str(doc))
        assert list(feed) == []
        assert feed.messages_seen == 1
        assert feed.messages_without_notam == 1
