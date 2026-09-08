"""What the platform keeps when it reads a State's site live.

The architecture reads the State's website and sends the user to the State's
own document. That settles where a document is read and where a user is sent.
It does not settle what is kept, and the difference is the whole of this
module: a citation that resolves only by re-fetching the State's URL resolves
for one AIRAC cycle. Qatar's expired-issues list is NIL.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aeropub.live import (
    KEEP_EXTRACT,
    KEEP_WHOLE,
    DocumentLink,
    Retention,
    retention_for,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


class TestWhatToKeep:

    @pytest.mark.parametrize(
        "media_type",
        ["text/html", "application/xml", "text/html; charset=UTF-8"],
    )
    def test_markup_we_parsed_is_kept_whole(self, media_type):
        assert retention_for(media_type, parsed=True) is Retention.ARCHIVED

    def test_a_parsed_pdf_keeps_its_text_not_its_megabytes(self):
        """A supplement published as PDF. Keeping it whole is waste; keeping
        nothing makes the facts drawn from it unverifiable."""
        assert retention_for("application/pdf", parsed=True) is Retention.EXTRACT_ONLY

    def test_a_chart_is_linked(self):
        """Nothing was drawn from it, and nobody wants our copy of a chart."""
        assert retention_for("image/png", parsed=False) is Retention.LINKED

    def test_nothing_drawn_from_it_means_nothing_kept(self):
        """Type does not matter when there is no citation to protect."""
        assert retention_for("text/html", parsed=False) is Retention.LINKED

    def test_an_unknown_type_we_parsed_is_kept(self):
        """Guessing wrong towards keeping costs bytes. Guessing wrong towards
        discarding costs the citation, and only one of those is recoverable."""
        assert retention_for("application/x-unheard-of", parsed=True) is (
            Retention.ARCHIVED
        )

    def test_the_two_policies_do_not_overlap(self):
        assert not (KEEP_WHOLE & KEEP_EXTRACT)


class TestALinkIsNotACitation:

    def test_a_linked_document_expires_with_the_state(self):
        link = DocumentLink(
            url="https://aim.gov.qa/AIP/x/graphics/OTHH-ADC.pdf",
            document="Aerodrome chart OTHH",
            media_type="application/pdf",
            retrieved_at=NOW,
            retention=Retention.LINKED,
        )
        assert link.citation_expires_with_the_state

    def test_an_archived_document_does_not(self):
        link = DocumentLink(
            url="https://aim.gov.qa/AIP/x/html/eAIP/QA-ENR-3.2-en-GB.html",
            document="AIP Qatar ENR 3.2",
            media_type="text/html",
            retrieved_at=NOW,
            retention=Retention.ARCHIVED,
            archive_key="sha256:abc",
        )
        assert not link.citation_expires_with_the_state
        assert link.holds_the_original_layout

    def test_extract_only_resolves_but_loses_the_layout(self):
        """The facts stay verifiable. The document as the State set it does not."""
        link = DocumentLink(
            url="https://aim.gov.qa/AIP/x/eSUP/QA-SUP-16-2026.pdf",
            document="AIP Qatar SUP 16/2026",
            media_type="application/pdf",
            retrieved_at=NOW,
            retention=Retention.EXTRACT_ONLY,
            archive_key="sha256:def",
        )
        assert not link.citation_expires_with_the_state
        assert not link.holds_the_original_layout


class TestTheRecordCannotOverstateItself:
    """A citation claiming a copy it does not hold is the dangerous direction."""

    def test_linked_with_an_archive_key_is_refused(self):
        with pytest.raises(ValueError, match="LINKED but an archive key"):
            DocumentLink(
                url="https://example.test/chart.pdf",
                document="chart",
                media_type="application/pdf",
                retrieved_at=NOW,
                retention=Retention.LINKED,
                archive_key="sha256:abc",
            )

    def test_archived_without_an_archive_key_is_refused(self):
        with pytest.raises(ValueError, match="no archive key"):
            DocumentLink(
                url="https://example.test/page.html",
                document="page",
                media_type="text/html",
                retrieved_at=NOW,
                retention=Retention.ARCHIVED,
            )

    def test_a_hash_is_kept_even_for_a_linked_document(self):
        """It is what catches a State republishing under the same URL."""
        link = DocumentLink(
            url="https://example.test/chart.pdf",
            document="chart",
            media_type="application/pdf",
            retrieved_at=NOW,
            retention=Retention.LINKED,
            content_hash="a" * 64,
        )
        assert link.content_hash == "a" * 64

    def test_an_empty_url_is_refused(self):
        with pytest.raises(ValueError):
            DocumentLink(
                url="  ",
                document="x",
                media_type="text/html",
                retrieved_at=NOW,
                retention=Retention.LINKED,
            )


class TestWhatTheUserIsTold:

    def test_the_description_says_which_promise_is_being_made(self):
        """"Open the official document" and "this is the copy we parsed" are
        different promises, and a screen that blurs them misleads."""
        linked = DocumentLink(
            url="https://aim.gov.qa/chart.pdf",
            document="Aerodrome chart OTHH",
            media_type="application/pdf",
            retrieved_at=NOW,
            retention=Retention.LINKED,
        ).describe()
        assert "linked only, nothing kept" in linked

        archived = DocumentLink(
            url="https://aim.gov.qa/QA-ENR-3.2-en-GB.html",
            document="AIP Qatar ENR 3.2",
            media_type="text/html",
            retrieved_at=NOW,
            retention=Retention.ARCHIVED,
            archive_key="sha256:abc",
        ).describe()
        assert "archived whole" in archived

    def test_the_state_url_is_always_offered(self):
        for retention, key in (
            (Retention.LINKED, None),
            (Retention.ARCHIVED, "sha256:abc"),
            (Retention.EXTRACT_ONLY, "sha256:abc"),
        ):
            link = DocumentLink(
                url="https://aim.gov.qa/doc.html",
                document="doc",
                media_type="text/html",
                retrieved_at=NOW,
                retention=retention,
                archive_key=key,
            )
            assert "aim.gov.qa" in link.describe()
