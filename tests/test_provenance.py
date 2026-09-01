"""Tests for SourceRef.

A note on the no-mock-data rule: it governs *source data* — no fabricated
aeronautical values may enter the product or stand in for a real publication.
It does not forbid constructing objects to test pure logic, which is what these
do. Nothing here is presented as, or reachable as, real aeronautical data.
"""

from datetime import date, datetime, timezone

import pytest

from aeropub.provenance import Confidence, SourceRef

DIGEST = "a" * 64
READ_AT = datetime(2026, 10, 11, 14, 23, tzinfo=timezone.utc)


def make_ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="FAA",
        document="NOTAM A2291/26",
        locator="Item E line 2",
        retrieved_at=READ_AT,
        content_hash=DIGEST,
        parser_id="notam-e-parser",
        parser_version="4.2",
    )
    fields.update(overrides)
    return SourceRef(**fields)


class TestRequiredFields:
    @pytest.mark.parametrize(
        "field", ["source_id", "document", "locator", "parser_id", "parser_version"]
    )
    @pytest.mark.parametrize("empty", ["", "   "])
    def test_identifying_strings_cannot_be_blank(self, field, empty):
        with pytest.raises(ValueError, match=field):
            make_ref(**{field: empty})

    def test_retrieved_at_must_be_a_datetime(self):
        with pytest.raises(TypeError, match="retrieved_at"):
            make_ref(retrieved_at=date(2026, 10, 11))

    def test_naive_timestamps_are_rejected(self):
        # An ambiguous timestamp in a citation defeats the purpose of citing.
        with pytest.raises(ValueError, match="timezone-aware"):
            make_ref(retrieved_at=datetime(2026, 10, 11, 14, 23))


class TestContentHash:
    def test_accepts_a_lowercase_sha256_digest(self):
        assert make_ref(content_hash="0" * 64).content_hash == "0" * 64

    @pytest.mark.parametrize(
        "bad",
        ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65],
        ids=["empty", "short", "uppercase", "non-hex", "63-chars", "65-chars"],
    )
    def test_rejects_anything_that_is_not_one(self, bad):
        with pytest.raises(ValueError, match="SHA-256"):
            make_ref(content_hash=bad)


class TestOptionalFields:
    def test_publication_details_may_be_unknown(self):
        ref = make_ref()
        assert ref.published_at is None
        assert ref.original_url is None
        assert ref.archive_key is None

    def test_confidence_defaults_to_high(self):
        assert make_ref().confidence is Confidence.HIGH

    def test_confidence_must_be_the_enum(self):
        with pytest.raises(TypeError, match="Confidence"):
            make_ref(confidence="high")


class TestDescribe:
    def test_reads_as_a_citation_a_human_can_check(self):
        described = make_ref(confidence=Confidence.MEDIUM).describe()
        assert "NOTAM A2291/26" in described
        assert "Item E line 2" in described
        assert "11 Oct 2026 1423Z" in described
        assert "notam-e-parser 4.2" in described
        assert "medium" in described


class TestImmutability:
    def test_a_citation_cannot_be_edited_after_the_fact(self):
        ref = make_ref()
        with pytest.raises(Exception):
            ref.document = "something else"  # type: ignore[misc]
