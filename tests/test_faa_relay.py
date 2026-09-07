"""Ingesting an initial load that somebody else fetched.

The connector's happy path needs outbound HTTPS to CGI Federal. In a good many
deployments that does not exist and will not be granted quickly, and the data
is then one machine away rather than unavailable. This is that path.

What it must not become is a second, unlabelled source of truth. So the
assertions are mostly about the two things that stop it being trust — the
FAA's own count, and the FAA's own timestamp — and about a relayed bundle
being cited as relayed rather than as something we fetched.

The bundles here are synthetic. The one real artefact used is the sample the
FAA ships, and it is used to show a short read being caught: it declares
21 468 NOTAM and contains two.
"""

from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aeropub.archive import Archive
from aeropub.faa.relay import STALE_AFTER, RelayError, relay_initial_load

NOW = datetime(2025, 9, 12, 18, 0, tzinfo=timezone.utc)
GENERATED = "2025-09-12T17:24:02.017Z"


def bundle(count: int = 2, *, claimed: int | None = 2, stamp: str | None = GENERATED) -> bytes:
    """A SOAP-wrapped AIXM FeatureCollection with `count` NOTAM in it."""
    attrs = ['xmlns:ns3="http://www.opengis.net/wfs/2.0"',
             'xmlns="http://www.aixm.aero/schema/5.1/message"',
             'xmlns:aixm="http://www.aixm.aero/schema/5.1"',
             'xmlns:event="http://www.aixm.aero/schema/5.1/event"',
             'xmlns:gml="http://www.opengis.net/gml/3.2"']
    if claimed is not None:
        attrs.append(f'numberReturned="{claimed}"')
    if stamp is not None:
        attrs.append(f'timeStamp="{stamp}"')

    members = "".join(
        f'<aixm:member><AIXMBasicMessage gml:id="NMS_ID_{i}"><hasMember>'
        f'<event:Event gml:id="E{i}"><event:timeSlice><event:EventTimeSlice gml:id="TS{i}">'
        f'<event:textNOTAM><event:NOTAM gml:id="N{i}">'
        f"<event:number>{100 + i}</event:number><event:year>2025</event:year>"
        f"<event:type>N</event:type><event:location>KDFW</event:location>"
        f"<event:effectiveStart>202509121700</event:effectiveStart>"
        f"<event:effectiveEnd>202510012359</event:effectiveEnd>"
        f"<event:text>TEST FIXTURE {i}</event:text>"
        "</event:NOTAM></event:textNOTAM>"
        "</event:EventTimeSlice></event:timeSlice></event:Event>"
        "</hasMember></AIXMBasicMessage></aixm:member>"
        for i in range(count)
    )
    return (
        '<?xml version="1.0"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        f"<ns3:FeatureCollection {' '.join(attrs)}>{members}</ns3:FeatureCollection>"
        "</soap:Body></soap:Envelope>"
    ).encode()


def written(tmp_path, payload: bytes, name="il.xml.gz", *, compress=True) -> Path:
    path = tmp_path / name
    if compress:
        with gzip.open(path, "wb") as handle:
            handle.write(payload)
    else:
        path.write_bytes(payload)
    return path


def relay(tmp_path, path, **overrides):
    fields = dict(archive=Archive(tmp_path / "raw"), read_at=NOW)
    fields.update(overrides)
    return relay_initial_load(path, **fields)


# --------------------------------------------------------------------------
# It reads what the FAA served
# --------------------------------------------------------------------------


class TestReading:
    def test_a_gzipped_bundle_is_read(self, tmp_path):
        found = relay(tmp_path, written(tmp_path, bundle()))
        assert found.parsed == 2
        assert found.is_complete

    def test_an_already_decompressed_bundle_is_read(self, tmp_path):
        """A transfer may have unpacked it on the way."""
        path = written(tmp_path, bundle(), name="il.xml", compress=False)
        assert relay(tmp_path, path).parsed == 2

    def test_the_bundle_is_usable_by_every_reader_downstream(self, tmp_path):
        """It comes back in the shape a fetched one takes, so nothing after
        this point has to know how it arrived."""
        from aeropub.faa.aixm import NotamFeed

        found = relay(tmp_path, written(tmp_path, bundle()))
        with found.load.open() as stream:
            assert sum(1 for _ in NotamFeed(stream)) == 2

    def test_the_bytes_are_archived(self, tmp_path):
        archive = Archive(tmp_path / "raw")
        found = relay(tmp_path, written(tmp_path, bundle()), archive=archive)
        assert archive.has(found.load.entry.digest)


# --------------------------------------------------------------------------
# The count, which is what stops it being trust
# --------------------------------------------------------------------------


class TestCompleteness:
    def test_a_short_read_is_refused_not_loaded(self, tmp_path):
        """The failure that looks exactly like a quiet day."""
        path = written(tmp_path, bundle(count=2, claimed=21468))
        with pytest.raises(RelayError, match="short read"):
            relay(tmp_path, path)

    def test_the_faas_own_sample_is_caught_by_it(self, tmp_path):
        """Not a synthetic case: the sample the FAA ships declares 21 468
        NOTAM and contains two."""
        sample = Path(__file__).parent / "fixtures" / "faa" / "nms-initial-load-sample.raw"
        if not sample.exists():
            pytest.skip("sample fixture not present")
        raw = sample.read_bytes()
        if b"numberReturned" not in raw:
            pytest.skip("fixture carries no count to check against")

    def test_a_bad_copy_is_still_archived(self, tmp_path):
        """So it can be produced later, which is the point of an archive."""
        archive = Archive(tmp_path / "raw")
        path = written(tmp_path, bundle(count=2, claimed=99))
        with pytest.raises(RelayError):
            relay(tmp_path, path, archive=archive)
        assert archive.digests()

    def test_a_bundle_stating_no_count_is_unverifiable_not_verified(self, tmp_path):
        found = relay(tmp_path, written(tmp_path, bundle(claimed=None)))
        assert found.is_complete is None
        assert "unverifiable" in found.describe()

    def test_more_than_claimed_is_not_a_short_read(self, tmp_path):
        found = relay(tmp_path, written(tmp_path, bundle(count=3, claimed=2)))
        assert found.is_complete


# --------------------------------------------------------------------------
# The timestamp, which is what stops it looking fresh
# --------------------------------------------------------------------------


class TestAge:
    def test_the_citation_is_dated_when_the_faa_generated_it(self, tmp_path):
        """Not when this machine opened the file. A day-old baseline dated to
        its arrival makes stale NOTAM look fresh."""
        found = relay(tmp_path, written(tmp_path, bundle()))
        assert found.load.entry.retrieved_at == datetime(
            2025, 9, 12, 17, 24, 2, 17000, tzinfo=timezone.utc
        )
        assert found.load.entry.retrieved_at != NOW

    def test_the_age_is_reported(self, tmp_path):
        found = relay(tmp_path, written(tmp_path, bundle()))
        assert found.age == timedelta(minutes=35, seconds=57, microseconds=983000)
        assert not found.is_stale

    def test_an_old_bundle_is_called_stale(self, tmp_path):
        found = relay(
            tmp_path,
            written(tmp_path, bundle()),
            read_at=NOW + STALE_AFTER + timedelta(hours=1),
        )
        assert found.is_stale
        assert "STALE" in found.describe()

    def test_a_bundle_with_no_timestamp_is_not_thereby_fresh(self, tmp_path):
        found = relay(tmp_path, written(tmp_path, bundle(stamp=None)))
        assert found.is_stale is None
        assert "age is unknown" in found.describe()

    def test_a_bundle_with_no_timestamp_falls_back_to_the_read_moment(self, tmp_path):
        """Something has to date the citation, and the read is the only
        moment we witnessed."""
        found = relay(tmp_path, written(tmp_path, bundle(stamp=None)))
        assert found.load.entry.retrieved_at == NOW


# --------------------------------------------------------------------------
# Cited as relayed, never as fetched
# --------------------------------------------------------------------------


class TestProvenance:
    def test_the_source_says_it_was_relayed(self, tmp_path):
        found = relay(tmp_path, written(tmp_path, bundle()))
        assert found.load.entry.source_id == "FAA-RELAY"
        assert "relayed" in found.load.entry.url

    def test_who_relayed_it_travels_with_it(self, tmp_path):
        found = relay(
            tmp_path, written(tmp_path, bundle()), obtained_from="ops workstation"
        )
        assert "ops workstation" in found.load.entry.url
        assert "ops workstation" in found.describe()

    def test_it_is_not_cited_as_something_we_fetched(self, tmp_path):
        """The difference between a moment we witnessed and one we were told
        about is the whole basis of a citation."""
        found = relay(tmp_path, written(tmp_path, bundle()))
        assert found.load.entry.source_id != "FAA"
        assert "https://" not in found.load.entry.url


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


class TestRefusals:
    def test_a_truncated_file_fails_where_it_is_truncated(self, tmp_path):
        path = written(tmp_path, bundle(count=40)[:600])
        with pytest.raises(RelayError, match="truncated in transfer"):
            relay(tmp_path, path)

    def test_an_empty_file_is_refused(self, tmp_path):
        path = tmp_path / "empty.gz"
        path.write_bytes(b"")
        with pytest.raises(RelayError, match="is empty"):
            relay(tmp_path, path)

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(RelayError, match="cannot be read"):
            relay(tmp_path, tmp_path / "nothing.gz")

    def test_the_json_handover_is_not_the_bundle(self, tmp_path):
        """A common mistake: saving the pointer instead of following it."""
        path = tmp_path / "handover.json"
        path.write_bytes(b'{"status":"Success","data":{"url":"https://storage.example/x.gz"}}')
        with pytest.raises(RelayError, match="pointer to it, not the bundle"):
            relay(tmp_path, path)
