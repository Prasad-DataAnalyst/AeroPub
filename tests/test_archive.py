"""Tests for the raw store.

Real bytes, real files, real hashing — nothing stubbed. The archive's whole
value is that what comes out is exactly what went in, so the tests exercise
that directly rather than trusting the implementation.
"""

from datetime import datetime, timedelta, timezone

import pytest

from aeropub.archive import Archive, digest_of
from aeropub.provenance import SourceRef

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def store(tmp_path) -> Archive:
    return Archive(tmp_path / "archive")


def put(archive, body=b"<html>AIP</html>", **kw):
    fields = dict(source_id="ot-aip", url="https://example.invalid/aip", retrieved_at=NOW)
    fields.update(kw)
    return archive.put(body, **fields)


class TestRoundTrip:
    def test_what_comes_out_is_what_went_in(self, tmp_path):
        archive = store(tmp_path)
        body = b"<html>\x00binary tail\xff</html>"
        entry = put(archive, body)
        assert archive.get(entry.digest) == body

    def test_the_digest_is_the_hash_of_the_content(self, tmp_path):
        body = b"declared distances"
        entry = put(store(tmp_path), body)
        assert entry.digest == digest_of(body)

    def test_size_is_recorded(self, tmp_path):
        body = b"x" * 4096
        assert put(store(tmp_path), body).size == 4096

    def test_missing_digest_raises_clearly(self, tmp_path):
        with pytest.raises(KeyError, match="nothing archived"):
            store(tmp_path).get("f" * 64)


class TestDeduplication:
    def test_the_same_content_is_stored_once(self, tmp_path):
        archive = store(tmp_path)
        put(archive, b"same")
        put(archive, b"same", retrieved_at=NOW + timedelta(days=1))
        assert len(archive) == 1

    def test_a_repeat_reports_when_it_was_first_seen(self, tmp_path):
        # How a caller tells "unchanged" from "new" without re-reading the blob.
        archive = store(tmp_path)
        first = put(archive, b"same")
        assert first.first_seen_at is None

        again = put(archive, b"same", retrieved_at=NOW + timedelta(days=1))
        assert again.first_seen_at == NOW

    def test_different_content_is_stored_separately(self, tmp_path):
        archive = store(tmp_path)
        put(archive, b"before")
        put(archive, b"after")
        assert len(archive) == 2


class TestImmutability:
    def test_re_storing_does_not_alter_the_original_bytes(self, tmp_path):
        archive = store(tmp_path)
        entry = put(archive, b"original")
        put(archive, b"original", source_id="someone-else", url="https://other.invalid")
        assert archive.get(entry.digest) == b"original"

    def test_first_seen_metadata_is_not_overwritten_by_a_later_fetch(self, tmp_path):
        archive = store(tmp_path)
        entry = put(archive, b"content")
        put(archive, b"content", retrieved_at=NOW + timedelta(days=30))
        assert archive.metadata(entry.digest)["first_seen_at"] == NOW.isoformat()

    def test_there_is_no_delete(self, tmp_path):
        # Not an oversight. An archive not kept cannot be recovered, so the
        # capability to prune is deliberately absent from the interface.
        assert not any(
            name for name in dir(Archive)
            if name in {"delete", "remove", "prune", "purge", "clear"}
        )


class TestIntegrity:
    def test_corruption_is_detected_rather_than_returned(self, tmp_path):
        archive = store(tmp_path)
        entry = put(archive, b"trustworthy")
        archive._blob_path(entry.digest).write_bytes(b"tampered")

        with pytest.raises(ValueError, match="corruption"):
            archive.get(entry.digest)

    def test_verify_reports_the_broken_digests(self, tmp_path):
        archive = store(tmp_path)
        good = put(archive, b"good")
        bad = put(archive, b"bad")
        archive._blob_path(bad.digest).write_bytes(b"changed")

        assert archive.verify() == [bad.digest]
        assert good.digest not in archive.verify()

    def test_partial_writes_are_not_visible(self, tmp_path):
        # A crash mid-write must not leave a truncated blob under a hash that
        # claims to be complete, so staging files are excluded from listings.
        archive = store(tmp_path)
        entry = put(archive, b"content")
        stray = archive._blob_path(entry.digest).with_name("abc.partial")
        stray.write_bytes(b"half")
        assert list(archive.digests()) == [entry.digest]


class TestCitation:
    def test_an_entry_becomes_a_source_ref(self, tmp_path):
        entry = put(store(tmp_path), b"AD 2.13 content")
        ref = entry.to_source_ref(
            document="AIP AD 2.13",
            locator="RWY 34L row",
            parser_id="ad2-parser",
            parser_version="1.0",
        )
        assert isinstance(ref, SourceRef)
        assert ref.content_hash == entry.digest
        assert ref.archive_key == entry.digest
        assert ref.original_url == "https://example.invalid/aip"
        assert ref.retrieved_at == NOW

    def test_the_citation_resolves_back_to_the_stored_bytes(self, tmp_path):
        archive = store(tmp_path)
        body = b"the exact bytes that were parsed"
        entry = put(archive, body)
        ref = entry.to_source_ref(
            document="AIP", locator="x", parser_id="p", parser_version="1"
        )
        assert archive.get(ref.archive_key) == body


class TestBookkeeping:
    def test_naive_timestamps_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="timezone-aware"):
            put(store(tmp_path), retrieved_at=datetime(2026, 9, 1, 12, 0))

    def test_an_empty_archive_reports_empty(self, tmp_path):
        archive = store(tmp_path)
        assert len(archive) == 0
        assert archive.total_bytes() == 0
        assert list(archive.digests()) == []

    def test_total_bytes_counts_blobs_not_metadata(self, tmp_path):
        archive = store(tmp_path)
        put(archive, b"x" * 100)
        put(archive, b"y" * 200)
        assert archive.total_bytes() == 300
