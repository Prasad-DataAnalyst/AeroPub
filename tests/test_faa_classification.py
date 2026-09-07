"""This platform reads international NOTAM, in ICAO format, out of AIXM.

That is an architectural commitment, not a default, and it has two edges the
code has to hold.

The first is that a request says ``INTERNATIONAL`` and the payload that comes
back says ``INTL``. Held only as a comment, that fact could not be acted on: a
filter written the obvious way matched nothing at all, silently, and an
international-only register came back empty.

The second is that unknown is not a match. A NOTAM carrying no classification
is not international, and it is not known not to be; admitting it to an
international-only register because it was not positively excluded is the
failure this whole codebase is built against, in miniature.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from aeropub.archive import Archive
from aeropub.faa.aixm import NotamFeed
from aeropub.faa.config import Classification
from aeropub.faa.register import take_feed

NOW = datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)


def message(location="OTHH", classification="INTL", icao=True) -> str:
    icao_block = (
        "<event:translation><event:NOTAMTranslation gml:id='T2'>"
        "<event:type>ICAO</event:type>"
        "<event:formattedText>A1234/26 NOTAMN\n"
        "Q) OTDF/QMRLC/IV/NBO/A/000/999/2516N05136E005\n"
        "A) OTHH B) 2609070600 C) 2609302359\n"
        "E) RWY 16L/34R CLSD</event:formattedText>"
        "</event:NOTAMTranslation></event:translation>"
        if icao
        else ""
    )
    extension = (
        f"<event:extension><fnse:EventExtension gml:id='X'>"
        f"<fnse:classification>{classification}</fnse:classification>"
        f"<fnse:icaoLocation>{location}</fnse:icaoLocation>"
        f"</fnse:EventExtension></event:extension>"
        if classification is not None
        else ""
    )
    return (
        "<aixm:member><AIXMBasicMessage gml:id='M'><hasMember>"
        "<event:Event gml:id='E'><event:timeSlice><event:EventTimeSlice gml:id='TS'>"
        "<event:textNOTAM><event:NOTAM gml:id='N'>"
        "<event:number>1234</event:number><event:year>2026</event:year>"
        f"<event:type>N</event:type><event:location>{location}</event:location>"
        "<event:effectiveStart>202609070600</event:effectiveStart>"
        "<event:effectiveEnd>202609302359</event:effectiveEnd>"
        "<event:text>RWY 16L/34R CLSD</event:text>"
        "<event:translation><event:NOTAMTranslation gml:id='T1'>"
        "<event:type>LOCAL_FORMAT</event:type>"
        f"<event:simpleText>{location} 09/1234 RWY 16L/34R CLSD</event:simpleText>"
        "</event:NOTAMTranslation></event:translation>"
        f"{icao_block}"
        "</event:NOTAM></event:textNOTAM>"
        f"{extension}"
        "</event:EventTimeSlice></event:timeSlice></event:Event>"
        "</hasMember></AIXMBasicMessage></aixm:member>"
    )


def feed(*messages: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        '<ns3:FeatureCollection xmlns:ns3="http://www.opengis.net/wfs/2.0"'
        ' xmlns="http://www.aixm.aero/schema/5.1/message"'
        ' xmlns:aixm="http://www.aixm.aero/schema/5.1"'
        ' xmlns:event="http://www.aixm.aero/schema/5.1/event"'
        ' xmlns:gml="http://www.opengis.net/gml/3.2"'
        ' xmlns:fnse="http://www.aixm.aero/schema/5.1/extensions/FAA/FNSE"'
        f' numberReturned="{len(messages)}" timeStamp="2026-09-07T06:00:00.000Z">'
        + "".join(messages)
        + "</ns3:FeatureCollection></soap:Body></soap:Envelope>"
    ).encode()


def taken(tmp_path, payload: bytes, **kwargs):
    archive = Archive(tmp_path / "raw")
    entry = archive.put(payload, source_id="TEST", url="file:test", retrieved_at=NOW)
    return take_feed(NotamFeed(io.BytesIO(payload)), entry, **kwargs)


# --------------------------------------------------------------------------
# Both spellings of the same thing
# --------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("INTERNATIONAL", Classification.INTERNATIONAL),
            ("INTL", Classification.INTERNATIONAL),
            ("intl", Classification.INTERNATIONAL),
            ("DOMESTIC", Classification.DOMESTIC),
            ("DOM", Classification.DOMESTIC),
            ("LMIL", Classification.LOCAL_MILITARY),
            ("LOCAL_MILITARY", Classification.LOCAL_MILITARY),
            ("FDC", Classification.FDC),
        ],
    )
    def test_either_spelling_reads_to_the_same_thing(self, text, expected):
        """A request says INTERNATIONAL and the payload says INTL."""
        assert Classification.read(text) is expected

    def test_the_request_and_payload_forms_are_both_available(self):
        assert Classification.INTERNATIONAL.request_form == "INTERNATIONAL"
        assert Classification.INTERNATIONAL.payload_form == "INTL"

    def test_an_unknown_classification_is_none_not_the_nearest_member(self):
        """One the FAA introduces after this build should read as not-known,
        which a caller can report."""
        assert Classification.read("QUANTUM") is None
        assert Classification.read("") is None
        assert Classification.read(None) is None

    def test_international_is_icao_format_by_definition(self):
        assert Classification.INTERNATIONAL.is_icao_format is True

    def test_faa_domestic_and_fdc_are_not(self):
        assert Classification.DOMESTIC.is_icao_format is False
        assert Classification.FDC.is_icao_format is False

    def test_military_varies_and_says_so(self):
        assert Classification.MILITARY.is_icao_format is None


# --------------------------------------------------------------------------
# What a NOTAM says about itself
# --------------------------------------------------------------------------


class TestTheNotam:
    def read(self, payload: bytes):
        return list(NotamFeed(io.BytesIO(payload)))[0]

    def test_an_international_notam_knows_it(self):
        found = self.read(feed(message(classification="INTL")))
        assert found.classification_read is Classification.INTERNATIONAL
        assert found.is_international is True

    def test_a_domestic_notam_knows_it(self):
        assert self.read(feed(message(classification="DOM"))).is_international is False

    def test_no_classification_is_not_thereby_international(self):
        """An application reading only international NOTAM has to tell 'not
        international' from 'we cannot say'."""
        found = self.read(feed(message(classification=None)))
        assert found.is_international is None
        assert found.classification_read is None

    def test_an_international_notam_carries_its_icao_reading(self):
        assert self.read(feed(message())).has_icao_reading

    def test_one_without_an_icao_translation_says_so(self):
        """Present but not screenable: no Q-line, so no FIR, no level band,
        no centre."""
        assert not self.read(feed(message(icao=False))).has_icao_reading


# --------------------------------------------------------------------------
# The filter
# --------------------------------------------------------------------------


class TestIntakeFilter:
    def test_only_international_is_admitted(self, tmp_path):
        payload = feed(
            message(classification="INTL"),
            message(location="KDFW", classification="DOM"),
        )
        intake = taken(tmp_path, payload, only=[Classification.INTERNATIONAL])
        assert intake.indexed == 1
        assert intake.excluded == ("1 DOMESTIC",)

    def test_the_payload_spelling_matches_the_request_spelling(self, tmp_path):
        """The defect this exists for: filtering on INTERNATIONAL against a
        payload saying INTL matched nothing, silently."""
        intake = taken(tmp_path, feed(message()), only=["INTERNATIONAL"])
        assert intake.indexed == 1

    def test_an_unclassified_notam_never_passes_a_filter(self, tmp_path):
        """Unknown is not a match."""
        intake = taken(
            tmp_path, feed(message(classification=None)),
            only=[Classification.INTERNATIONAL],
        )
        assert intake.indexed == 0
        assert intake.without_classification == 1

    def test_what_was_filtered_is_counted_not_dropped(self, tmp_path):
        """A register holding four hundred of twenty thousand is either a
        correct filter or a broken one, and only the counts tell them apart."""
        payload = feed(*[message(location="KDFW", classification="DOM")] * 3)
        intake = taken(tmp_path, payload, only=[Classification.INTERNATIONAL])
        assert intake.read == 3
        assert intake.indexed == 0
        assert "3 DOMESTIC" in intake.describe()

    def test_no_filter_admits_everything(self, tmp_path):
        payload = feed(message(classification="INTL"), message(classification="DOM"))
        assert taken(tmp_path, payload).indexed == 2

    def test_an_unknown_classification_in_the_filter_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown NOTAM classification"):
            taken(tmp_path, feed(message()), only=["QUANTUM"])

    def test_an_admitted_notam_with_no_icao_reading_is_counted(self, tmp_path):
        """It is in the register and it cannot be screened, and those are
        different enough to say separately."""
        intake = taken(
            tmp_path, feed(message(icao=False)), only=[Classification.INTERNATIONAL]
        )
        assert intake.indexed == 1
        assert intake.without_icao_reading == 1
        assert "printable, not screenable" in intake.describe()

    def test_register_feed_still_returns_a_register(self, tmp_path):
        """The older entry point is unchanged for callers that only want the
        register."""
        from aeropub.faa.register import register_feed

        archive = Archive(tmp_path / "raw")
        payload = feed(message())
        entry = archive.put(payload, source_id="T", url="file:t", retrieved_at=NOW)
        register = register_feed(
            NotamFeed(io.BytesIO(payload)), entry, only=["INTERNATIONAL"]
        )
        assert len(register) == 1
